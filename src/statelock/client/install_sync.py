# SPDX-License-Identifier: Apache-2.0
"""Make sync Playwright's own file APIs work through Statelock: ``install()`` for
``playwright.sync_api``.

    import statelock.client
    statelock.client.install_sync()

After ``install_sync()``, on a browser connected through Statelock, ``set_input_files``
(Page, Locator, ElementHandle) and ``FileChooser.set_files`` upload through Statelock,
and ``page.expect_download()`` returns the download Statelock checked (default timeout:
the page's or context's ``set_default_timeout``, else 30 s). ``page.request`` is not
covered for the sync API.
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Any

from playwright.sync_api import BrowserContext, ElementHandle, FileChooser, Locator, Page

from statelock.client import files_sync, patching, transfer
from statelock.client.files_sync import set_input_files_sync, statelock_session_sync


class _ExpectDownloadSync:
    """page.expect_download(): Statelock's checked download on a governed page,
    Playwright's own everywhere else."""

    def __init__(self, page: Page, predicate: Any, timeout: float | None) -> None:
        self._page = page
        self._predicate = predicate
        self._timeout = timeout
        self._inner: contextlib.AbstractContextManager[Any] | None = None

    def __enter__(self) -> Any:
        if statelock_session_sync(self._page):
            self._inner = files_sync.expect_download_sync(self._page, self._predicate, timeout=self._timeout)
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


def _page_set_input_files(self: Page, selector: str, files: Any, **options: Any) -> None:
    if statelock_session_sync(self):
        set_input_files_sync(self, selector, files, timeout=transfer.upload_timeout(options))
        return
    _patches.original(Page, "set_input_files")(self, selector, files, **options)


def _locator_set_input_files(self: Locator, files: Any, **options: Any) -> None:
    if statelock_session_sync(self.page):
        set_input_files_sync(self.page, self, files, timeout=transfer.upload_timeout(options))
        return
    _patches.original(Locator, "set_input_files")(self, files, **options)


def _handle_set_input_files(self: ElementHandle, files: Any, **options: Any) -> None:
    frame = self.owner_frame()
    page = frame.page if frame is not None else None
    if page is not None and statelock_session_sync(page):
        set_input_files_sync(page, self, files, timeout=transfer.upload_timeout(options))
        return
    _patches.original(ElementHandle, "set_input_files")(self, files, **options)


def _chooser_set_files(self: FileChooser, files: Any, **options: Any) -> None:
    if statelock_session_sync(self.page):
        set_input_files_sync(self.page, self.element, files, timeout=transfer.upload_timeout(options))
        return
    _patches.original(FileChooser, "set_files")(self, files, **options)


def _page_expect_download(self: Page, predicate: Any = None, timeout: float | None = None) -> _ExpectDownloadSync:
    return _ExpectDownloadSync(self, predicate, timeout)


def _page_set_default_timeout(self: Page, timeout: float) -> None:
    patching.record_default_timeout(self, timeout)
    _patches.original(Page, "set_default_timeout")(self, timeout)


def _context_set_default_timeout(self: BrowserContext, timeout: float) -> None:
    patching.record_default_timeout(self, timeout)
    _patches.original(BrowserContext, "set_default_timeout")(self, timeout)


_patches = patching.Patches(
    (
        (Page, "set_input_files", _page_set_input_files),
        (Locator, "set_input_files", _locator_set_input_files),
        (ElementHandle, "set_input_files", _handle_set_input_files),
        (FileChooser, "set_files", _chooser_set_files),
        (Page, "expect_download", _page_expect_download),
        (Page, "set_default_timeout", _page_set_default_timeout),
        (BrowserContext, "set_default_timeout", _context_set_default_timeout),
    )
)


def install_sync() -> None:
    """Route sync Playwright's upload and download APIs through Statelock (idempotent)."""
    _patches.install()


def uninstall_sync() -> None:
    """Restore sync Playwright's own methods."""
    _patches.uninstall()
    files_sync.forget_sessions()
