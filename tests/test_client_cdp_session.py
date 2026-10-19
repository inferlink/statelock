"""The SDK's short-lived CDP session: detached afterwards, unless Statelock ended the session."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from statelock.client import files, files_sync
from statelock.client.violations import StatelockPolicyViolationError
from statelock.wire import VIOLATION_MARKER


class _Session:
    def __init__(self, *, hangs: bool = False) -> None:
        self.hangs = hangs
        self.detached = 0

    async def detach(self) -> None:
        self.detached += 1
        if self.hangs:  # Playwright's detach on a connection that is closing
            await asyncio.Event().wait()


class _SyncSession:
    def __init__(self) -> None:
        self.detached = 0

    def detach(self) -> None:
        self.detached += 1


class _Browser:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


class _Page:
    def __init__(self, session: Any, browser: _Browser | None = None) -> None:
        self.context = self
        self.session = session
        self.browser = browser or _Browser()

    async def new_cdp_session(self, _page: Any) -> Any:
        return self.session


class _SyncPage(_Page):
    def new_cdp_session(self, _page: Any) -> Any:  # type: ignore[override]
        return self.session


def test_a_session_statelock_ended_is_not_detached() -> None:
    session = _Session(hangs=True)

    async def use() -> None:
        async with files.cdp_session(_Page(session)):  # type: ignore[arg-type]
            raise RuntimeError(f"Protocol error: {VIOLATION_MARKER} blocked")

    with pytest.raises(RuntimeError, match=VIOLATION_MARKER):
        asyncio.run(asyncio.wait_for(use(), timeout=2))
    assert session.detached == 0


def test_a_detach_that_never_answers_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "DETACH_TIMEOUT", 0.05)
    session = _Session(hangs=True)

    async def use() -> str:
        async with files.cdp_session(_Page(session)):  # type: ignore[arg-type]
            return "done"

    assert asyncio.run(asyncio.wait_for(use(), timeout=2)) == "done"
    assert session.detached == 1


def test_other_errors_still_detach() -> None:
    session = _Session()

    async def use() -> None:
        async with files.cdp_session(_Page(session)):  # type: ignore[arg-type]
            raise ValueError("not a violation")

    with pytest.raises(ValueError, match="not a violation"):
        asyncio.run(use())
    assert session.detached == 1


def test_sync_session_statelock_ended_is_not_detached() -> None:
    session = _SyncSession()
    with pytest.raises(RuntimeError), files_sync.cdp_session(_SyncPage(session)):  # type: ignore[arg-type]
        raise RuntimeError(f"Protocol error: {VIOLATION_MARKER} blocked")
    assert session.detached == 0
    with files_sync.cdp_session(_SyncPage(session)):  # type: ignore[arg-type]
        pass
    assert session.detached == 1


def test_a_blocked_download_is_not_detached() -> None:
    # A blocked download ends the session; its error carries no marker text.
    session = _Session(hangs=True)

    async def use() -> None:
        async with files.cdp_session(_Page(session)):  # type: ignore[arg-type]
            raise StatelockPolicyViolationError({"rule": "restrict_downloads"})

    with pytest.raises(StatelockPolicyViolationError):
        asyncio.run(asyncio.wait_for(use(), timeout=2))
    assert session.detached == 0


def test_sync_session_on_a_lost_connection_is_not_detached() -> None:
    session = _SyncSession()
    with files_sync.cdp_session(_SyncPage(session, _Browser(connected=False))):  # type: ignore[arg-type]
        pass
    assert session.detached == 0
