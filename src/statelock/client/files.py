# SPDX-License-Identifier: Apache-2.0
"""Governed uploads and downloads over the agent's CDP connection (async Playwright)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

from playwright.async_api import CDPSession, ElementHandle, Locator, Page
from playwright.async_api import Error as PlaywrightError

from statelock.client import transfer
from statelock.client.transfer import DownloadInfo, StatelockDownloadError, UploadFile

T = TypeVar("T")


@contextlib.asynccontextmanager
async def cdp_session(page: Page) -> AsyncIterator[CDPSession]:
    """A short-lived CDP session on the page (Statelock.* commands run on it)."""
    session = await page.context.new_cdp_session(page)
    try:
        yield session
    finally:
        with contextlib.suppress(PlaywrightError):
            await session.detach()


async def run(steps: transfer.Steps[T], cdp: CDPSession, write: Callable[[bytes], Awaitable[None]] | None = None) -> T:
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
                await asyncio.sleep(step.seconds)
            elif isinstance(step, transfer.Write):
                assert write is not None  # noqa: S101 - only read_steps writes
                await write(step.data)
            else:
                result = await cdp.send(step.method, step.params)
        except Exception as raised:  # noqa: BLE001 - the steps decide what a failed command means
            error = raised


# Uploads ------------------------------------------------------------------------------------


async def set_input_files(
    page: Page,
    target: str | Locator | ElementHandle,
    files: UploadFile | Sequence[UploadFile],
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Upload files to Statelock, then set them on a file input with DOM.setFileInputFiles.

    ``target`` is a selector, Locator or ElementHandle for an ``<input type=file>`` in
    the page's main frame; ``timeout`` (ms) bounds the wait for a selector or Locator.
    Returns what Statelock stored for each file (path, name, size, sha256, mimeType).
    """
    locator = page.locator(target) if isinstance(target, str) else target
    wait = {"timeout": timeout} if timeout is not None and isinstance(locator, Locator) else {}
    payloads = await asyncio.to_thread(transfer.file_payloads, files)
    marker = transfer.new_marker()
    async with cdp_session(page) as cdp:
        uploaded = await run(transfer.upload_steps(payloads), cdp)
        # Find the input's DOM node for this CDP session through a temporary attribute.
        await locator.evaluate(transfer.SET_MARKER_SCRIPT, marker, **wait)
        try:
            await run(transfer.set_file_input_steps(marker, uploaded), cdp)
        finally:
            with contextlib.suppress(PlaywrightError):
                await locator.evaluate(transfer.REMOVE_MARKER_SCRIPT)
    return uploaded


# Downloads ----------------------------------------------------------------------------------


@dataclass
class StatelockDownload(DownloadInfo):
    """A download that passed Statelock's checks, with Playwright's async Download API
    (``save_as``, ``path``, ``suggested_filename``, ``url``), so code written for
    ``page.expect_download`` works."""

    page: Page | None = None
    _saved: Path | None = None

    async def path(self) -> Path:
        """The file on this machine (fetched from Statelock into a temporary file once)."""
        if self._saved is None:
            self._saved = await self.save_as(await asyncio.to_thread(transfer.temporary_path, self.name))
        return self._saved

    async def failure(self) -> str | None:
        return None  # a download that failed or was blocked is never handed over

    async def delete(self) -> None:
        if self._saved is not None:
            await asyncio.to_thread(functools.partial(self._saved.unlink, missing_ok=True))
            self._saved = None

    async def cancel(self) -> None:
        """Nothing to cancel: the download already completed and passed Statelock's checks."""

    async def _stream(self, write: Callable[[bytes], Awaitable[None]]) -> None:
        assert self.page is not None  # noqa: S101 - set by expect_download
        async with cdp_session(self.page) as cdp:
            await run(transfer.read_steps(self), cdp, write)

    async def read_bytes(self) -> bytes:
        chunks: list[bytes] = []

        async def collect(chunk: bytes) -> None:
            chunks.append(chunk)

        await self._stream(collect)
        return b"".join(chunks)

    async def save_as(self, path: str | Path) -> Path:
        """Stream the file to path. A partial file is removed if the integrity check fails."""
        target = Path(path)
        handle: BinaryIO = await asyncio.to_thread(target.open, "wb")

        async def write(chunk: bytes) -> None:
            await asyncio.to_thread(handle.write, chunk)

        try:
            await self._stream(write)
        except BaseException:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(functools.partial(target.unlink, missing_ok=True))
            raise
        await asyncio.to_thread(handle.close)
        return target


class DownloadWaiter:
    """What ``expect_download`` yields; ``value`` (awaitable, like Playwright's
    ``EventContextManager.value``) is the download after the block."""

    def __init__(self) -> None:
        self._download: StatelockDownload | None = None

    @property
    async def value(self) -> StatelockDownload:
        if self._download is None:
            raise StatelockDownloadError("the download is only available after the expect_download block")
        return self._download


@contextlib.asynccontextmanager
async def expect_download(
    page: Page,
    timeout: float,
    on_blocked: Callable[[transfer.DownloadBlocked], Awaitable[BaseException]],
    predicate: Callable[[StatelockDownload], bool] | None = None,
) -> AsyncIterator[DownloadWaiter]:
    """Wait for the next download the block starts, after Statelock has checked it.
    ``timeout`` is in seconds. Raises ``await on_blocked(...)`` if Statelock blocked it."""
    waiter = DownloadWaiter()
    async with cdp_session(page) as cdp:
        known = await run(transfer.known_downloads_steps(), cdp)
        yield waiter
        deadline = time.monotonic() + timeout
        while True:
            try:
                info = await run(transfer.wait_for_download_steps(known, max(0, deadline - time.monotonic())), cdp)
            except transfer.DownloadBlocked as blocked:
                raise await on_blocked(blocked) from None
            download = StatelockDownload(**vars(info), page=page)
            if predicate is None or predicate(download):
                waiter._download = download
                return
            known.add(info.guid)
