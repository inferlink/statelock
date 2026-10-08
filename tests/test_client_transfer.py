"""The upload and download protocol shared by the async and sync clients (statelock.client.transfer)."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from statelock.client import StatelockPolicyViolationError, patching, transfer
from statelock.client.transfer import DownloadBlocked, StatelockDownloadError

JS_MIME_TABLE = Path(__file__).resolve().parents[1] / "js" / "src" / "mime.ts"


def test_upload_payloads_and_chunks(tmp_path: Path) -> None:
    file = tmp_path / "a.pdf"
    file.write_bytes(b"%PDF")
    assert transfer.file_payload(file) == ("a.pdf", "application/pdf", b"%PDF")
    for name, expected in (("paper.TEX", "application/x-tex"), ("a.zip", "application/zip"), ("noext", None)):
        (tmp_path / name).write_bytes(b"x")
        assert transfer.file_payload(tmp_path / name)[1] == expected
    assert transfer.file_payload({"name": "n.txt", "mimeType": "text/plain", "buffer": "hi"}) == (
        "n.txt",
        "text/plain",
        b"hi",
    )
    assert transfer.file_payload({"name": "raw", "buffer": bytearray(b"\x00\xff")})[2] == b"\x00\xff"
    assert transfer.file_payload({"name": "view", "buffer": memoryview(b"abc")[1:]})[2] == b"bc"
    assert transfer.file_payload({"name": "text", "buffer": "é"})[2] == "é".encode()
    assert transfer.upload_items("x") == ["x"] and transfer.upload_items(["x", "y"]) == ["x", "y"]
    data = b"x" * (transfer.CHUNK_BYTES + 1)
    chunks = list(transfer.upload_chunks(data))
    assert len(chunks) == 2 and b"".join(base64.b64decode(c) for c in chunks) == data
    assert list(transfer.upload_chunks(b"")) == []


def test_marker_node() -> None:
    assert transfer.marked_node_id({"nodeId": 7}) == 7
    with pytest.raises(ValueError, match="main frame"):
        transfer.marked_node_id({"nodeId": 0})


def test_download_states() -> None:
    done = {"guid": "g", "state": "completed", "name": "r.pdf", "url": "u", "size": 3, "sha256": "s"}
    assert transfer.settle({"guid": "g", "state": "in_progress"}) is None
    info = transfer.settle(done)
    assert info is not None and (info.guid, info.suggested_filename, info.size) == ("g", "r.pdf", 3)
    with pytest.raises(DownloadBlocked, match="not allowed"):
        transfer.settle({"guid": "b", "state": "blocked", "reason": "not allowed"})
    with pytest.raises(StatelockDownloadError, match="canceled"):
        transfer.settle({"guid": "c", "state": "canceled", "name": "c"})
    assert transfer.first_new_download([done], known={"g"}) is None
    assert transfer.download_entries({"downloads": [done, "junk"]}) == [done]


def test_digest_check() -> None:
    data = b"report"
    info = transfer.DownloadInfo("g", "r", "u", len(data), hashlib.sha256(data).hexdigest())
    check = transfer.DigestCheck(info)
    assert check.chunk({"data": base64.b64encode(data).decode(), "eof": False}) == (data, False)
    assert check.chunk({"data": "", "eof": False}) == (b"", True)
    check.verify()
    bad = transfer.DigestCheck(transfer.DownloadInfo("g", "r", "u", 1, "0" * 64))
    bad.chunk({"data": base64.b64encode(b"x").decode(), "eof": True})
    with pytest.raises(StatelockDownloadError, match="SHA-256"):
        bad.verify()


def test_patches() -> None:
    class Target:
        def method(self) -> str:
            return "original"

    patches = patching.Patches(((Target, "method", lambda _self: "patched"),))
    patches.install()
    patches.install()  # idempotent: the original is kept
    assert Target().method() == "patched" and patches.original(Target, "method")(Target()) == "original"
    patches.uninstall()
    assert Target().method() == "original"


def test_read_and_wait_steps() -> None:
    data = b"report"
    info = transfer.DownloadInfo("g", "r", "u", len(data), hashlib.sha256(data).hexdigest())
    steps = transfer.read_steps(info)
    assert next(steps) == transfer.Command("Statelock.downloadRead", transfer.read_request("g", 0))
    assert steps.send({"data": base64.b64encode(data).decode(), "eof": True}) == transfer.Write(data)
    with pytest.raises(StopIteration):
        steps.send(None)

    wait = transfer.wait_for_download_steps({"old"}, timeout=60)
    assert next(wait) == transfer.Command("Statelock.downloads")
    assert wait.send({"downloads": [{"guid": "old", "state": "completed"}]}) == transfer.Sleep(0.1)
    next(wait)
    with pytest.raises(DownloadBlocked):
        wait.send({"downloads": [{"guid": "new", "state": "blocked", "reason": "no"}]})
    with pytest.raises(StatelockDownloadError, match="within"):
        next(transfer.wait_for_download_steps(set(), timeout=0))


@pytest.mark.parametrize("buffer", [None, 7, ["a"], Path("a.txt")])
def test_upload_buffer_of_another_type_is_refused(buffer: object) -> None:
    """Not uploaded as its str() or as an empty file: the recorded SHA-256 would be of that."""
    with pytest.raises(TypeError, match="buffer must be bytes or str"):
        transfer.file_payload({"name": "x", "buffer": buffer})


def test_an_upload_without_a_buffer_is_refused() -> None:
    with pytest.raises(TypeError, match="buffer must be bytes or str"):
        transfer.file_payloads([{"name": "x"}])


def test_blocked_download_reports_the_servers_rule() -> None:
    blocked = DownloadBlocked({"guid": "b", "state": "blocked", "reason": "no", "rule": "restrict_downloads"})
    violation = transfer.blocked_violation(blocked, "s")
    assert isinstance(violation, StatelockPolicyViolationError)
    assert violation.violation == {"rule": "restrict_downloads", "reason": "no", "session_id": "s"}
    unnamed = DownloadBlocked({"guid": "b", "state": "blocked", "reason": "no"})
    assert transfer.blocked_violation(unnamed, None).rule is None


def test_download_timeout_is_in_milliseconds_and_defaults_to_the_pages() -> None:
    assert transfer.download_timeout_seconds(None) == 30.0
    assert transfer.download_timeout_seconds(1500) == 1.5
    assert transfer.download_timeout_seconds(0) == float("inf")
    assert transfer.download_timeout_seconds(None, 360_000) == 360.0  # set_default_timeout
    assert transfer.download_timeout_seconds(1500, 360_000) == 1.5  # an explicit timeout wins
    assert transfer.download_timeout_seconds(None, 0) == float("inf")


def test_default_timeouts_resolve_like_playwrights() -> None:
    class Owner:
        pass

    context, page = Owner(), Owner()
    page.context = context  # type: ignore[attr-defined]
    assert patching.default_timeout_ms(page) is None
    patching.record_default_timeout(context, 5000)
    assert patching.default_timeout_ms(page) == 5000.0  # the context's, until the page sets its own
    patching.record_default_timeout(page, 700)
    assert patching.default_timeout_ms(page) == 700.0


def _settled(guid: str, state: str = "completed", **extra: Any) -> dict[str, Any]:
    return {"guid": guid, "state": state, "name": f"{guid}.pdf", "url": "u", "size": 1, "sha256": "s", **extra}


def test_expect_download_steps_skip_what_the_predicate_refuses() -> None:
    steps = transfer.expect_download_steps({"old"}, 60, lambda info: info.name if info.guid == "b" else None, "s-1")
    assert next(steps) == transfer.Command("Statelock.downloads")
    # "a" is refused and remembered; "b" is taken.
    assert steps.send({"downloads": [_settled("old"), _settled("a")]}) == transfer.Command("Statelock.downloads")
    with pytest.raises(StopIteration) as done:
        steps.send({"downloads": [_settled("a"), _settled("b")]})
    assert done.value.value == "b.pdf"


def test_expect_download_steps_raise_a_blocked_download_as_a_violation() -> None:
    steps = transfer.expect_download_steps(set(), 60, lambda info: info, "s-1")
    next(steps)
    with pytest.raises(StatelockPolicyViolationError) as raised:
        steps.send({"downloads": [_settled("x", "blocked", reason="no .exe", rule="restrict_downloads")]})
    assert (raised.value.rule, raised.value.reason, raised.value.session_id) == ("restrict_downloads", "no .exe", "s-1")


def test_reading_a_download_before_the_block_ends_is_a_clear_error() -> None:
    waiter: transfer.Waiter[str] = transfer.Waiter()
    with pytest.raises(RuntimeError, match="after the expect_download block"):
        waiter.result()
    waiter.resolve("done")
    assert waiter.result() == "done"


def test_the_mime_table_is_the_js_sdks() -> None:
    """Upload evidence must not depend on the SDK: js/src/mime.ts holds the same table."""
    if not JS_MIME_TABLE.exists():  # the Docker test image mounts only the Python sources
        pytest.skip("js/src/mime.ts is not available")
    js = dict(re.findall(r'"(\.[a-z0-9]+)": "([^"]+)"', JS_MIME_TABLE.read_text(encoding="utf-8")))
    assert js == transfer.MIME_TYPES
