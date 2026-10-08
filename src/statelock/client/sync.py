# SPDX-License-Identifier: Apache-2.0
"""Make sync Playwright's own file APIs work through Statelock: ``install()`` for
``playwright.sync_api``.

    import statelock.client
    statelock.client.install_sync()

After ``install_sync()``, on a browser connected through Statelock, ``set_input_files``
(Page, Locator, ElementHandle) and ``FileChooser.set_files`` upload through Statelock,
and ``page.expect_download()`` returns the download Statelock checked. ``page.request``
is not covered for the sync API. The protocol is shared with the async client
(transfer.py); this module adds the blocking transport (``run``) and the sync patches.
"""

from __future__ import annotations

import contextlib
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from playwright.sync_api import CDPSession, ElementHandle, FileChooser, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from statelock.client import transfer
from statelock.client.transfer import DownloadInfo, StatelockDownloadError, UploadFile
from statelock.client.violations import StatelockPolicyViolationError

T = TypeVar("T")

_governed: weakref.WeakKeyDictionary[Any, dict[str, Any] | None] = weakref.WeakKeyDictionary()


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
    if key not in _governed:
        with cdp_session(page) as cdp:
            # Only an answer is cached: an error (a closed page) raises and is asked again next time.
            _governed[key] = run(transfer.session_steps(), cdp)
    return _governed[key]


# Uploads ------------------------------------------------------------------------------------


def set_input_files_sync(
    page: Page,
    target: str | Locator | ElementHandle,
    files: UploadFile | Sequence[UploadFile],
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


class DownloadWaiterSync:
    """What ``page.expect_download()`` yields on a governed page; ``value`` after the block."""

    def __init__(self) -> None:
        self._download: StatelockDownloadSync | None = None

    @property
    def value(self) -> StatelockDownloadSync:
        if self._download is None:
            raise StatelockDownloadError("the download is only available after the expect_download block")
        return self._download


@contextlib.contextmanager
def _expect_download(
    page: Page,
    timeout: float,
    session_id: str | None,
    predicate: Callable[[StatelockDownloadSync], bool] | None = None,
) -> Iterator[DownloadWaiterSync]:
    waiter = DownloadWaiterSync()
    with cdp_session(page) as cdp:
        known = run(transfer.known_downloads_steps(), cdp)
        yield waiter
        deadline = time.monotonic() + timeout
        while True:
            try:
                info = run(transfer.wait_for_download_steps(known, max(0, deadline - time.monotonic())), cdp)
            except transfer.DownloadBlocked as blocked:
                raise StatelockPolicyViolationError(transfer.blocked_violation(blocked, session_id)) from None
            download = StatelockDownloadSync(**vars(info), page=page)
            if predicate is None or predicate(download):
                waiter._download = download
                return
            known.add(info.guid)


class _ExpectDownloadSync:
    """page.expect_download(): Statelock's checked download on a governed page,
    Playwright's own everywhere else."""

    def __init__(self, page: Page, predicate: Any, timeout: float | None) -> None:
        self._page = page
        self._predicate = predicate
        self._timeout = timeout
        self._inner: contextlib.AbstractContextManager[Any] | None = None

    def __enter__(self) -> Any:
        session = statelock_session_sync(self._page)
        if session:
            seconds = transfer.download_timeout_seconds(self._timeout)
            self._inner = _expect_download(self._page, seconds, session.get("session_id"), self._predicate)
        else:
            self._inner = _patches.original(Page, "expect_download")(
                self._page, predicate=self._predicate, timeout=self._timeout
            )
        return self._inner.__enter__()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        assert self._inner is not None  # noqa: S101 - set in __enter__
        return self._inner.__exit__(exc_type, exc, tb)


# Patches ------------------------------------------------------------------------------------


def _page_set_input_files(self: Page, selector: str, files: Any, **options: Any) -> None:
    if statelock_session_sync(self):
        set_input_files_sync(self, selector, files, transfer.upload_timeout(options))
        return
    _patches.original(Page, "set_input_files")(self, selector, files, **options)


def _locator_set_input_files(self: Locator, files: Any, **options: Any) -> None:
    if statelock_session_sync(self.page):
        set_input_files_sync(self.page, self, files, transfer.upload_timeout(options))
        return
    _patches.original(Locator, "set_input_files")(self, files, **options)


def _handle_set_input_files(self: ElementHandle, files: Any, **options: Any) -> None:
    frame = self.owner_frame()
    page = frame.page if frame is not None else None
    if page is not None and statelock_session_sync(page):
        set_input_files_sync(page, self, files, transfer.upload_timeout(options))
        return
    _patches.original(ElementHandle, "set_input_files")(self, files, **options)


def _chooser_set_files(self: FileChooser, files: Any, **options: Any) -> None:
    if statelock_session_sync(self.page):
        set_input_files_sync(self.page, self.element, files, transfer.upload_timeout(options))
        return
    _patches.original(FileChooser, "set_files")(self, files, **options)


def _page_expect_download(self: Page, predicate: Any = None, timeout: float | None = None) -> _ExpectDownloadSync:
    return _ExpectDownloadSync(self, predicate, timeout)


_patches = transfer.Patches(
    (
        (Page, "set_input_files", _page_set_input_files),
        (Locator, "set_input_files", _locator_set_input_files),
        (ElementHandle, "set_input_files", _handle_set_input_files),
        (FileChooser, "set_files", _chooser_set_files),
        (Page, "expect_download", _page_expect_download),
    )
)


def install_sync() -> None:
    """Route sync Playwright's upload and download APIs through Statelock (idempotent)."""
    _patches.install()


def uninstall_sync() -> None:
    """Restore sync Playwright's own methods."""
    _patches.uninstall()
    _governed.clear()
