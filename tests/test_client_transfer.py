"""The upload and download protocol shared by the async and sync clients (statelock.client.transfer)."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from statelock.client import transfer
from statelock.client.transfer import DownloadBlocked, StatelockDownloadError


def test_upload_payloads_and_chunks(tmp_path: Path) -> None:
    file = tmp_path / "a.pdf"
    file.write_bytes(b"%PDF")
    assert transfer.file_payload(file) == ("a.pdf", "application/pdf", b"%PDF")
    assert transfer.file_payload({"name": "n.txt", "mimeType": "text/plain", "buffer": "hi"}) == (
        "n.txt",
        "text/plain",
        b"hi",
    )
    assert transfer.file_payload({"name": "raw", "buffer": bytearray(b"\x00\xff")})[2] == b"\x00\xff"
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


def test_timeouts_and_patches() -> None:
    assert transfer.download_timeout_seconds(None) == 30.0
    assert transfer.download_timeout_seconds(0) == float("inf")
    assert transfer.download_timeout_seconds(1500) == 1.5

    class Target:
        def method(self) -> str:
            return "original"

    patches = transfer.Patches(((Target, "method", lambda _self: "patched"),))
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
