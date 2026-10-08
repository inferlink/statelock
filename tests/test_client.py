"""Client SDK pieces that need no browser."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from playwright.async_api import Error as PlaywrightError

from statelock.client import (
    SessionUrl,
    SessionUrlError,
    StatelockConnection,
    StatelockPolicyViolationError,
    create_session_url,
    statelock_guard,
    statelock_guard_sync,
    transfer,
)
from statelock.client.files import run
from statelock.client.sync import run as run_sync
from statelock.wire import VIOLATION_MARKER


class _Statelock:
    """A stand-in for Statelock's HTTP API: GET /violations/<id> answers from ``answers``."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, dict[str, Any]]] = {}
        self.requests: list[tuple[str, str | None]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stub.requests.append((self.path, self.headers.get("Authorization")))
                status, body = stub.answers.get(self.path, (404, {"detail": "No violation recorded for session"}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def session(self, session_id: str = "s-1", api_key: str | None = "slk_session") -> SessionUrl:
        return SessionUrl(session_id, "agent", f"{self.url}/sessions/slt_x", "ws://unused", "t", api_key=api_key)


@pytest.fixture
def statelock() -> Iterator[_Statelock]:
    stub = _Statelock()
    thread = threading.Thread(target=stub.server.serve_forever, daemon=True)
    thread.start()
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


RECORDED = {
    "rule": "restrict_downloads",
    "reason": "tool.exe: .exe is not allowed",
    "session_id": "s-1",
    "agent_id": "a",
}


def test_connection_headers_for_frameworks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATELOCK_API_KEY", "slk_env")
    conn = StatelockConnection("ws://proxy/statelock", "agent", session_id="s-1")
    assert conn.headers == {
        "x-statelock-agent-id": "agent",
        "x-statelock-session-id": "s-1",
        "Authorization": "Bearer slk_env",
    }
    assert "Authorization" not in StatelockConnection("ws://p", "agent", api_key="").headers
    with pytest.raises(RuntimeError, match="not connected"):
        _ = conn.browser
    browser = object()
    conn.attach(browser)  # type: ignore[arg-type]
    assert conn.browser is browser


def test_an_unreachable_server_is_a_session_url_error() -> None:
    with socket.socket() as sock:  # a free port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(SessionUrlError, match="could not reach Statelock") as raised:
        create_session_url(server_url=f"http://127.0.0.1:{port}", api_key="k")
    assert raised.value.status is None
    with pytest.raises(SessionUrlError, match="could not reach Statelock"):
        SessionUrl("s", "a", f"http://127.0.0.1:{port}/sessions/x", "ws://x", "t").violation()


# Violation lookups ---------------------------------------------------------------------------


def test_violation_lookup_uses_the_sessions_key_and_404_means_none(
    statelock: _Statelock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STATELOCK_API_KEY", "slk_env")
    session = statelock.session()
    assert session.violation() is None
    statelock.answers["/violations/s-1"] = (200, RECORDED)
    found = session.violation()
    assert found is not None and (found.rule, found.agent_id) == ("restrict_downloads", "a")
    assert session.violation("slk_other") is not None
    assert statelock.session(api_key=None).violation() is not None
    assert [auth for _, auth in statelock.requests] == [
        "Bearer slk_session",
        "Bearer slk_session",
        "Bearer slk_other",
        "Bearer slk_env",  # no session key: STATELOCK_API_KEY
    ]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_refused_lookup_is_an_error_not_no_violation(statelock: _Statelock, status: int) -> None:
    statelock.answers["/violations/s-1"] = (status, {"detail": "no"})
    with pytest.raises(SessionUrlError, match=f"refused the request \\({status}\\)") as raised:
        statelock.session().violation()
    assert raised.value.status == status


def test_guards_pass_ordinary_errors_through_without_a_lookup(statelock: _Statelock) -> None:
    async def guarded() -> None:
        async with statelock_guard(statelock.url, "s-1", "k"):
            raise PlaywrightError("Timeout 30000ms exceeded")

    with pytest.raises(PlaywrightError, match="Timeout"):
        asyncio.run(guarded())
    with pytest.raises(PlaywrightError, match="Timeout"), statelock_guard_sync(statelock.url, "s-1", "k"):
        raise PlaywrightError("Timeout 30000ms exceeded")
    assert statelock.requests == []


def test_guards_name_the_recorded_violation(statelock: _Statelock) -> None:
    statelock.answers["/violations/s-1"] = (200, RECORDED)

    async def guarded() -> None:
        async with statelock_guard(statelock.url.replace("http", "ws") + "/statelock", "s-1", "k"):
            raise PlaywrightError("Target page, context or browser has been closed")

    with pytest.raises(StatelockPolicyViolationError) as raised:
        asyncio.run(guarded())
    assert raised.value.rule == "restrict_downloads"
    with pytest.raises(StatelockPolicyViolationError) as raised, statelock_guard_sync(statelock.url, "s-1", "k"):
        raise PlaywrightError("Target page, context or browser has been closed")
    assert raised.value.rule == "restrict_downloads"


def test_without_a_record_the_marker_in_the_error_is_used(statelock: _Statelock) -> None:
    marked = f'{VIOLATION_MARKER} {{"rule":"assert_field_equal","reason":"amounts differ","session_id":"s-1"}}'
    with pytest.raises(StatelockPolicyViolationError) as raised, statelock_guard_sync(statelock.url, "s-1", "k"):
        raise PlaywrightError(marked)
    assert (raised.value.rule, len(statelock.requests)) == ("assert_field_equal", 1)
    # A closed session with neither a record nor a marker is not a violation.
    with pytest.raises(PlaywrightError), statelock_guard_sync(statelock.url, "s-1", "k"):
        raise PlaywrightError("Target page, context or browser has been closed")


def test_guards_raise_when_statelock_refuses_the_lookup(statelock: _Statelock) -> None:
    statelock.answers["/violations/s-1"] = (401, {"detail": "invalid key"})
    with pytest.raises(SessionUrlError, match="401"), statelock_guard_sync(statelock.url, "s-1", "wrong"):
        raise PlaywrightError("Target page, context or browser has been closed")


def test_guards_complete_an_sdk_raised_violation(statelock: _Statelock) -> None:
    """A blocked download raises a violation with only rule and reason: the guard adds the record."""
    blocked = StatelockPolicyViolationError({"rule": "download", "reason": "tool.exe", "session_id": "s-1"})

    async def guarded() -> None:
        async with statelock_guard(statelock.url, None, "k"):
            raise blocked

    with pytest.raises(StatelockPolicyViolationError) as raised:
        asyncio.run(guarded())
    assert raised.value.rule == "download" and raised.value.__cause__ is None  # no record yet: unchanged
    statelock.answers["/violations/s-1"] = (200, RECORDED)
    with pytest.raises(StatelockPolicyViolationError) as raised:
        asyncio.run(guarded())
    assert (raised.value.rule, raised.value.agent_id, raised.value.__cause__) == ("restrict_downloads", "a", blocked)
    with pytest.raises(StatelockPolicyViolationError) as raised, statelock_guard_sync(statelock.url, "s-1", "k"):
        raise blocked
    assert raised.value.reason == RECORDED["reason"]


# Protocol drivers ----------------------------------------------------------------------------


class _Cdp:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.sent: list[str] = []

    def send(self, method: str, _params: dict[str, Any] | None = None) -> Any:
        self.sent.append(method)
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _AsyncCdp(_Cdp):
    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:  # type: ignore[override]
        return _Cdp.send(self, method, params)


def test_both_drivers_run_the_same_steps() -> None:
    unknown = PlaywrightError("Protocol error (Statelock.session): 'Statelock.session' wasn't found")
    closed = PlaywrightError("Target page, context or browser has been closed")
    governed = {"Statelock.session": {"session_id": "s", "agent_id": "a"}}
    for answers, expected in ((governed, governed["Statelock.session"]), ({"Statelock.session": unknown}, None)):
        assert run_sync(transfer.session_steps(), _Cdp(answers)) == expected  # type: ignore[arg-type]
        assert asyncio.run(run(transfer.session_steps(), _AsyncCdp(answers))) == expected  # type: ignore[arg-type]
    # Only the unknown-command answer means a plain browser; other errors are raised.
    with pytest.raises(PlaywrightError, match="closed"):
        run_sync(transfer.session_steps(), _Cdp({"Statelock.session": closed}))  # type: ignore[arg-type]
    with pytest.raises(PlaywrightError, match="closed"):
        asyncio.run(run(transfer.session_steps(), _AsyncCdp({"Statelock.session": closed})))  # type: ignore[arg-type]

    uploads = {
        "Statelock.uploadFileBegin": {"uploadId": "u"},
        "Statelock.uploadFileChunk": {},
        "Statelock.uploadFileEnd": {"path": "/p/a.txt", "name": "a.txt", "size": 2, "sha256": "x", "mimeType": None},
    }
    cdp = _Cdp(uploads)
    stored = run_sync(transfer.upload_steps([("a.txt", None, b"hi")]), cdp)  # type: ignore[arg-type]
    assert [file["path"] for file in stored] == ["/p/a.txt"]
    assert cdp.sent == ["Statelock.uploadFileBegin", "Statelock.uploadFileChunk", "Statelock.uploadFileEnd"]


def test_upload_options() -> None:
    assert transfer.upload_timeout({}) is None
    assert transfer.upload_timeout({"timeout": 500, "no_wait_after": True, "strict": True}) == 500.0
    with pytest.raises(ValueError, match="strict=False"):
        transfer.upload_timeout({"strict": False})
    with pytest.raises(TypeError, match="unexpected options force"):
        transfer.upload_timeout({"force": True})


def test_a_downloads_temporary_file_stays_in_the_temporary_folder(tmp_path: Path) -> None:
    path = transfer.temporary_path("../../etc/evil.pdf")
    try:
        assert path.parent == Path(transfer.tempfile.gettempdir()) and path.name.endswith("-evil.pdf")
    finally:
        path.unlink()
