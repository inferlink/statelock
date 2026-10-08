import base64
import json
import os
from pathlib import Path

import pytest
from helpers import context_for, mouse_action, run

from statelock.audit import SCHEMA_VERSION, LocalJsonSink, build_action_record, build_network_record
from statelock.core.state import BrowserState
from statelock.core.verdict import PolicyVerdict

SESSION = "11111111-2222-3333-4444-555555555555"


def _record(sequence: int = 1, screenshot: bytes | None = b"img"):
    state = BrowserState(
        url="https://example.test",
        page_text="card 4111111111111111",
        screenshot_base64=base64.b64encode(screenshot).decode() if screenshot else None,
    )
    ctx = context_for(mouse_action(), state=state, sequence=sequence, session_id=SESSION)
    return build_action_record(ctx, PolicyVerdict.allow("ok"))


def test_record_extracts_screenshots_and_redacts() -> None:
    record = _record()
    assert record.schema_version == SCHEMA_VERSION
    assert record.screenshots == {"screenshot.jpg": b"img"}
    state = record.payload["context"]["browser_state"]
    assert state["screenshot_file"] == "screenshot.jpg"
    assert "screenshot_base64" not in state
    assert state["page_text"] == "card [REDACTED]"
    # A 12-digit UUID group is not treated as a card number.
    assert record.payload["context"]["session_id"] == SESSION
    assert state["dom_snapshot"] == {"redacted": True, "reason": "large_capture_payload_omitted"}


def test_record_without_screenshot() -> None:
    record = _record(screenshot=None)
    assert record.screenshots == {}
    assert record.payload["context"]["browser_state"]["screenshot_file"] is None


def test_local_sink_roundtrip(tmp_path: Path) -> None:
    sink = LocalJsonSink(tmp_path)
    assert not sink.session_exists(SESSION)
    run(sink.write_action(_record(1)))
    run(sink.write_action(_record(2)))
    assert sink.session_exists(SESSION)
    assert run(sink.list_actions(SESSION)) == [1, 2]
    assert run(sink.read_action(SESSION, 1))["schema_version"] == SCHEMA_VERSION
    assert run(sink.read_screenshot(SESSION, 2, "screenshot.jpg")) == b"img"
    assert run(sink.read_screenshot(SESSION, 2, "../x")) is None
    assert run(sink.read_action(SESSION, 9)) is None
    assert not (tmp_path / "latest.json").exists()


def test_local_sink_writes_whole_files(tmp_path: Path) -> None:
    """Records are renamed into place, so a reader never sees a partial context.json and no
    temporary files stay behind."""
    run(LocalJsonSink(tmp_path).write_action(_record(1)))
    action_dir = tmp_path / "sessions" / SESSION / "action-0001"
    assert sorted(path.name for path in action_dir.iterdir()) == ["context.json", "screenshot.jpg"]


def test_local_sink_failed_write_leaves_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(source: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        run(LocalJsonSink(tmp_path).write_action(_record(1)))
    action_dir = tmp_path / "sessions" / SESSION / "action-0001"
    assert list(action_dir.iterdir()) == []


def test_local_sink_latest_only_in_debug(tmp_path: Path) -> None:
    run(LocalJsonSink(tmp_path, write_latest=True).write_action(_record()))
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["sequence"] == 1


def test_network_log(tmp_path: Path) -> None:
    sink = LocalJsonSink(tmp_path)
    run(sink.append_network_record(SESSION, build_network_record({"url": "https://x.test/?e=someone@example.com"})))
    line = json.loads((tmp_path / "sessions" / SESSION / "network.jsonl").read_text().strip())
    assert line["url"] == "https://x.test/?e=[REDACTED]"
    assert "recorded_at" in line


def test_network_log_read_back(tmp_path: Path) -> None:
    sink = LocalJsonSink(tmp_path)
    assert run(sink.read_network_records(SESSION)) == []
    run(sink.append_network_record(SESSION, {"url": "https://a.test/", "recorded_at": "fixed"}))
    run(sink.append_network_record(SESSION, {"url": "https://b.test/"}))
    path = tmp_path / "sessions" / SESSION / "network.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    records = run(sink.read_network_records(SESSION))
    assert [r["url"] if r else None for r in records] == ["https://a.test/", "https://b.test/", None]
    assert records[0] is not None
    assert records[0]["recorded_at"] == "fixed"  # a caller-supplied timestamp is kept


def test_mask_secret_input() -> None:
    from statelock.audit.redaction import SECRET_MARKER, mask_secret_input

    typed = {"type": "keyDown", "key": "h", "code": "KeyH", "text": "h", "windowsVirtualKeyCode": 72}
    masked = mask_secret_input(typed, secret_target=True)
    assert {masked[k] for k in ("key", "code", "text", "windowsVirtualKeyCode")} == {SECRET_MARKER}
    assert masked["type"] == "keyDown"
    assert masked["statelock_redacted"] == "secret_input"
    assert typed["key"] == "h"  # the original (sent to the browser) is untouched
    enter = {"type": "keyUp", "key": "Enter", "code": "Enter"}
    assert mask_secret_input(enter, secret_target=True) == enter
    assert mask_secret_input({"text": "hunter2"}, secret_target=True)["text"] == SECRET_MARKER
    assert mask_secret_input({"text": "hunter2"}, secret_target=False)["text"] == "hunter2"


def test_secret_target_detection() -> None:
    from statelock.core.state import TargetElement

    assert TargetElement(input_type="password").is_secret_input
    assert TargetElement(input_type="text", autocomplete="one-time-code").is_secret_input
    assert TargetElement(input_type="text", autocomplete="section-pay cc-number").is_secret_input
    assert not TargetElement(input_type="text", autocomplete="username").is_secret_input


def test_redaction_keeps_uuids_paths_and_digests() -> None:
    from statelock.audit.redaction import redact_payload

    payload = {
        "path": "/srv/statelock-uploads-x/883f5a2e-1c2d-4e5f-9a8b-123456789012/a.pdf",
        "url": "https://x.test/883f5a2e-1c2d-4e5f-9a8b-123456789012/f",
        "note": "card 4111111111111111 and id 883f5a2e-1c2d-4e5f-9a8b-123456789012",
        "guid": "123456789012345",
    }
    redacted = redact_payload(payload)
    assert redacted["path"] == payload["path"]
    assert redacted["url"] == payload["url"]
    assert redacted["guid"] == payload["guid"]
    assert redacted["note"] == "card [REDACTED] and id 883f5a2e-1c2d-4e5f-9a8b-123456789012"


@pytest.mark.parametrize(
    "text",
    [
        "card 4111 1111 1111 1111 on file",
        "card 4111-1111-1111-1111.",
        "card 5500005555555559",
        "amex 3782 822463 10005",
        "SSN 123-45-6789",
        "SSN 123 45 6789",
        "SSN 123456789.",
    ],
)
def test_card_numbers_and_ssns_are_redacted_in_any_format(text: str) -> None:
    from statelock.audit.redaction import redact_text

    redacted = redact_text(text)
    assert "[REDACTED]" in redacted
    assert not any(char.isdigit() for char in redacted)


@pytest.mark.parametrize(
    "text",
    [
        "order 4111 1111 1111 1112",  # fails the Luhn check
        "id 883f5a2e-1c2d-4e5f-9a8b-123456789012",
        "/srv/uploads/123456789/a.pdf",
        "report-123456789.pdf",
        "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "never issued 000-12-3456 666-12-3456 900-12-3456 123-00-4567 123-45-0000",
        "total $1,234,567.89 on 2026-10-02",
    ],
)
def test_redaction_leaves_other_numbers(text: str) -> None:
    from statelock.audit.redaction import redact_text

    assert redact_text(text) == text


def test_write_atomic_leaves_no_temporary_file_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from statelock.fileio import write_atomic

    target = tmp_path / "context.json"
    write_atomic(target, b"old")

    def fail(_source: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, b"new", mode=0o600)
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["context.json"]


def test_write_atomic_creates_private_files_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from statelock.fileio import write_atomic

    modes: list[int] = []
    real_open = os.open

    def recording_open(path: str, flags: int, mode: int = 0o777) -> int:
        modes.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", recording_open)
    write_atomic(tmp_path / "secret", b"x", mode=0o600)
    assert modes == [0o600]  # owner-only from the moment the file exists
    assert (tmp_path / "secret").stat().st_mode & 0o777 == 0o600
