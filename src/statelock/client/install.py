# SPDX-License-Identifier: Apache-2.0
"""Make Playwright's own file APIs work through Statelock, with no other code changes.

    import statelock.client
    statelock.client.install()

After ``install()``, on a browser connected through Statelock (by any framework):

- ``set_input_files`` (Page, Locator, ElementHandle) and ``FileChooser.set_files``
  upload the files through Statelock, as governed uploads;
- ``page.expect_download()`` waits for the download Statelock checked, and its
  value works like Playwright's Download (``save_as``, ``path``,
  ``suggested_filename``, ``url``). Its default timeout is the page's
  ``set_default_timeout`` (or its context's) when set after ``install()``, else 30 s;
- ``page.request`` and ``context.request`` make their requests through
  Statelock, in the page with the browser's session, if the agent's policy
  allows them (``request_access``; see statelock.client.requests).

Other browsers are untouched: the first call on a browser asks it whether it is a
Statelock session (the ``Statelock.session`` command) and remembers the answer.
Playwright's async API; ``install_sync()`` (install_sync.py) covers the sync API.
``page.on("download")`` listeners are not covered.
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Any

from playwright.async_api import BrowserContext, ElementHandle, FileChooser, Locator, Page

from statelock.client import files as client_files
from statelock.client import patching, transfer
from statelock.client.files import statelock_session
from statelock.client.requests import GovernedRequestContext


async def _page_set_input_files(self: Page, selector: str, files: Any, **options: Any) -> None:
    if await statelock_session(self):
        await client_files.set_input_files(self, selector, files, timeout=transfer.upload_timeout(options))
        return
    await _patches.original(Page, "set_input_files")(self, selector, files, **options)


async def _locator_set_input_files(self: Locator, files: Any, **options: Any) -> None:
    if await statelock_session(self.page):
        await client_files.set_input_files(self.page, self, files, timeout=transfer.upload_timeout(options))
        return
    await _patches.original(Locator, "set_input_files")(self, files, **options)


async def _handle_page(handle: ElementHandle) -> Page | None:
    frame = await handle.owner_frame()
    return frame.page if frame is not None else None


async def _handle_set_input_files(self: ElementHandle, files: Any, **options: Any) -> None:
    page = await _handle_page(self)
    if page is not None and await statelock_session(page):
        await client_files.set_input_files(page, self, files, timeout=transfer.upload_timeout(options))
        return
    await _patches.original(ElementHandle, "set_input_files")(self, files, **options)


async def _chooser_set_files(self: FileChooser, files: Any, **options: Any) -> None:
    if await statelock_session(self.page):
        await client_files.set_input_files(self.page, self.element, files, timeout=transfer.upload_timeout(options))
        return
    await _patches.original(FileChooser, "set_files")(self, files, **options)


class _ExpectDownload:
    """page.expect_download(): Statelock's checked download on a governed page,
    Playwright's own everywhere else."""

    def __init__(self, page: Page, predicate: Any, timeout: float | None) -> None:
        self._page = page
        self._predicate = predicate
        self._timeout = timeout
        self._inner: contextlib.AbstractAsyncContextManager[Any] | None = None

    async def __aenter__(self) -> Any:
        if await statelock_session(self._page):
            self._inner = client_files.expect_download(self._page, self._predicate, timeout=self._timeout)
        else:
            self._inner = _patches.original(Page, "expect_download")(
                self._page, predicate=self._predicate, timeout=self._timeout
            )
        return await self._inner.__aenter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        assert self._inner is not None  # noqa: S101 - set in __aenter__
        return await self._inner.__aexit__(exc_type, exc, tb)


def _page_expect_download(self: Page, predicate: Any = None, timeout: float | None = None) -> _ExpectDownload:
    return _ExpectDownload(self, predicate, timeout)


def _page_request(self: Page) -> GovernedRequestContext:
    return GovernedRequestContext(lambda: self, _patches.original(Page, "request").fget(self))


def _context_request(self: BrowserContext) -> GovernedRequestContext:
    return GovernedRequestContext(
        lambda: self.pages[0] if self.pages else None,
        _patches.original(BrowserContext, "request").fget(self),
    )


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
        (Page, "request", property(_page_request)),
        (BrowserContext, "request", property(_context_request)),
    )
)


def install() -> None:
    """Route Playwright's upload, download and request APIs through Statelock (idempotent)."""
    _patches.install()


def uninstall() -> None:
    """Restore Playwright's own methods."""
    _patches.uninstall()
    client_files.forget_sessions()
