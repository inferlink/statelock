# SPDX-License-Identifier: Apache-2.0
"""Governed uploads and downloads over the agent's CDP connection (sync Playwright).

The same API as files.py, for ``playwright.sync_api``; the protocol is shared
(transfer.py), this module adds the blocking transport (``run``).
"""

from __future__ import annotations

import contextlib
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from playwright.sync_api import CDPSession, ElementHandle, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from statelock.client import patching, transfer
from statelock.client.transfer import DownloadInfo, UploadFile

T = TypeVar("T")

# Per browser (or context without one): its Statelock.session answer.
_sessions: weakref.WeakKeyDictionary[Any, dict[str, Any] | None] = weakref.WeakKeyDictionary()


@contextlib.contextmanager
def cdp_session(page: Page) -> Iterator[CDPSession]:
    """A short-lived CDP session on the page (Statelock.* commands run on it)."""
    session = page.context.new_cdp_session(page)
    try:
        yield session
    finally:
        with contextlib.suppress(PlaywrightError):
            session.detach()


def run(steps: transfer.Steps[T], cdp: CDPSession, write: Callable[[bytes], Any] | None = None) -> T:
    """Perform protocol steps over cdp; a failed command's error goes back into the steps."""
    result: Any = None
    error: Exception | None = None
    while True:
        try:
            step = steps.throw(error) if error is not None else steps.send(result)
        except StopIteration as done:
            return done.value  # type: ignore[no-any-return]
        result, error = None, None
        try:
            if isinstance(step, transfer.Sleep):
                time.sleep(step.seconds)
            elif isinstance(step, transfer.Write):
                assert write is not None  # noqa: S101 - only read_steps writes
                write(step.data)
            else:
                result = cdp.send(step.method, step.params)
        except Exception as raised:  # noqa: BLE001 - the steps decide what a failed command means
            error = raised


def statelock_session_sync(page: Page) -> dict[str, Any] | None:
    """{session_id, agent_id} if the page's browser is connected through Statelock, else None."""
    key = page.context.browser or page.context
    if key not in _sessions:
        with cdp_session(page) as cdp:
            # Only an answer is cached: an error (a closed page) raises and is asked again next time.
            _sessions[key] = run(transfer.session_steps(), cdp)
    return _sessions[key]


def forget_sessions() -> None:
    """Forget which browsers are Statelock sessions (uninstall_sync)."""
    _sessions.clear()


# Uploads ------------------------------------------------------------------------------------


def set_input_files_sync(
    page: Page,
    target: str | Locator | ElementHandle,
    files: UploadFile | Sequence[UploadFile],
    *,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """``set_input_files`` (files.py) for Playwright's sync API."""
    locator = page.locator(target) if isinstance(target, str) else target
    wait = {"timeout": timeout} if timeout is not None and isinstance(locator, Locator) else {}
    payloads = transfer.file_payloads(files)
    marker = transfer.new_marker()
    with cdp_session(page) as cdp:
        uploaded = run(transfer.upload_steps(payloads), cdp)
        locator.evaluate(transfer.SET_MARKER_SCRIPT, marker, **wait)
        try:
            run(transfer.set_file_input_steps(marker, uploaded), cdp)
        finally:
            with contextlib.suppress(PlaywrightError):
                locator.evaluate(transfer.REMOVE_MARKER_SCRIPT)
    return uploaded


# Downloads ----------------------------------------------------------------------------------


@dataclass
class StatelockDownloadSync(DownloadInfo):
    """A download that passed Statelock's checks, with sync Playwright's Download API."""

    page: Page | None = None
    _saved: Path | None = None

    def path(self) -> Path:
        """The file on this machine (fetched from Statelock into a temporary file once)."""
        if self._saved is None:
            self._saved = self.save_as(transfer.temporary_path(self.name))
        return self._saved

    def failure(self) -> str | None:
        return None  # a download that failed or was blocked is never handed over

    def delete(self) -> None:
        if self._saved is not None:
            self._saved.unlink(missing_ok=True)
            self._saved = None

    def cancel(self) -> None:
        """Nothing to cancel: the download already completed and passed Statelock's checks."""

    def _stream(self, write: Callable[[bytes], Any]) -> None:
        assert self.page is not None  # noqa: S101 - set by expect_download
        with cdp_session(self.page) as cdp:
            run(transfer.read_steps(self), cdp, write)

    def read_bytes(self) -> bytes:
        chunks: list[bytes] = []
        self._stream(chunks.append)
        return b"".join(chunks)

    def save_as(self, path: str | Path) -> Path:
        """Stream the file to path. A partial file is removed if the integrity check fails."""
        target = Path(path)
        try:
            with target.open("wb") as handle:
                self._stream(handle.write)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target


class DownloadWaiterSync(transfer.Waiter[StatelockDownloadSync]):
    """What ``expect_download_sync`` yields; ``value`` is the download after the block."""

    @property
    def value(self) -> StatelockDownloadSync:
        return self.result()


@contextlib.contextmanager
def expect_download_sync(
    page: Page,
    predicate: Callable[[StatelockDownloadSync], bool] | None = None,
    *,
    timeout: float | None = None,
) -> Iterator[DownloadWaiterSync]:
    """``expect_download`` (files.py) for Playwright's sync API."""
    session = statelock_session_sync(page)
    if not session:
        raise RuntimeError("expect_download: the page's browser is not connected through Statelock")
    seconds = transfer.download_timeout_seconds(timeout, patching.default_timeout_ms(page))

    def accept(info: DownloadInfo) -> StatelockDownloadSync | None:
        download = StatelockDownloadSync(**vars(info), page=page)
        return download if predicate is None or predicate(download) else None

    waiter = DownloadWaiterSync()
    with cdp_session(page) as cdp:
        known = run(transfer.known_downloads_steps(), cdp)
        yield waiter
        waiter.resolve(run(transfer.expect_download_steps(known, seconds, accept, session.get("session_id")), cdp))


class FileMethodsSync:
    """``set_input_files`` and ``expect_download`` on a governed sync session object;
    ``page`` defaults to the session's first page."""

    @property
    def page(self) -> Page:
        raise NotImplementedError

    def set_input_files(
        self,
        target: str | Locator | ElementHandle,
        files: UploadFile | Sequence[UploadFile],
        *,
        page: Page | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Upload files through Statelock into a file input (see ``set_input_files_sync``)."""
        return set_input_files_sync(page or self.page, target, files, timeout=timeout)

    def expect_download(
        self,
        predicate: Callable[[StatelockDownloadSync], bool] | None = None,
        *,
        page: Page | None = None,
        timeout: float | None = None,
    ) -> AbstractContextManager[DownloadWaiterSync]:
        """The next download Statelock checked (see ``expect_download_sync``)."""
        return expect_download_sync(page or self.page, predicate, timeout=timeout)
