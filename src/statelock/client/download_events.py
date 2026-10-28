# SPDX-License-Identifier: Apache-2.0
"""``page.on("download")`` on a Statelock page (async Playwright, after ``install()``).

On a governed page Playwright's own download event never fires: Statelock, not the
agent, receives the browser's downloads. So each page with download listeners gets
one watcher task: it polls ``Statelock.downloads`` over one CDP session and hands
each new completed download that began in one of the page's frames to the listeners,
as a StatelockDownload. A download Statelock blocked ends the session, so it is never
handed over. A download Chromium gives no frame for goes to the page only when it is
the only page of its browser context with download listeners.

The watcher stops when the last listener is removed, the page closes or the session
ends. Listeners on a plain browser are Playwright's alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import CDPSession, Page

from statelock.client import files, transfer
from statelock.client.files import StatelockDownload
from statelock.client.violations import session_ended

logger = logging.getLogger(__name__)

Handler = Callable[[StatelockDownload], Any]


@dataclass
class _Listeners:
    """A page's download listeners (handler, once) and its watcher."""

    handlers: list[tuple[Handler, bool]] = field(default_factory=list)
    watcher: asyncio.Task[None] | None = None


_pages: weakref.WeakKeyDictionary[Page, _Listeners] = weakref.WeakKeyDictionary()
_tasks: set[asyncio.Task[Any]] = set()  # strong references until each task is done


def _keep(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def add(page: Page, handler: Handler, *, once: bool = False) -> None:
    """Register a download listener; start the page's watcher if it has none."""
    listeners = _pages.setdefault(page, _Listeners())
    listeners.handlers.append((handler, once))
    if listeners.watcher is None or listeners.watcher.done():
        listeners.watcher = _keep(asyncio.get_running_loop().create_task(_watch(page, listeners)))


def remove(page: Page, handler: Handler) -> None:
    """Remove a download listener (its last registration, as pyee does); the watcher
    stops with the last one."""
    listeners = _pages.get(page)
    if listeners is None:
        return
    for index in range(len(listeners.handlers) - 1, -1, -1):
        if listeners.handlers[index][0] == handler:
            del listeners.handlers[index]
            break
    if not listeners.handlers:
        _stop(page, listeners)


def _stop(page: Page, listeners: _Listeners) -> None:
    if listeners.watcher is not None and listeners.watcher is not asyncio.current_task():
        listeners.watcher.cancel()
    listeners.watcher = None
    if _pages.get(page) is listeners and not listeners.handlers:
        del _pages[page]


def stop_all() -> None:
    """Stop every watcher and forget the listeners (uninstall)."""
    for page, listeners in list(_pages.items()):
        listeners.handlers.clear()
        _stop(page, listeners)


def _only_listening_page(page: Page) -> bool:
    """Whether no other page of the page's browser context has download listeners."""
    return all(other is page or not _pages.get(other) or not _pages[other].handlers for other in page.context.pages)


def _deliver(listeners: _Listeners, download: StatelockDownload) -> None:
    """Call each listener, as Playwright does: a coroutine runs in its own task, errors are logged."""
    for entry in list(listeners.handlers):
        handler, once = entry
        if once:
            with contextlib.suppress(ValueError):
                listeners.handlers.remove(entry)
        try:
            # As Playwright does: a listener that takes no argument is called without one.
            result = handler(*[download][: len(inspect.signature(handler).parameters)])
            if inspect.isawaitable(result):
                _keep(asyncio.ensure_future(result)).add_done_callback(_log_failure)
        except Exception:
            logger.exception("page.on('download') listener failed")


def _log_failure(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("page.on('download') listener failed", exc_info=task.exception())


async def _watch(page: Page, listeners: _Listeners) -> None:
    try:
        if not await files.statelock_session(page):
            return  # a plain browser: Playwright's own download event fires
        async with files.cdp_session(page) as cdp:
            await _poll(page, listeners, cdp)
    except Exception as error:  # noqa: BLE001 - a watcher never raises; it stops
        if not (session_ended(error) or page.is_closed()):
            logger.warning("page.on('download') stopped: %s", error)
    finally:
        if listeners.watcher is asyncio.current_task():
            _stop(page, listeners)


async def _poll(page: Page, listeners: _Listeners, cdp: CDPSession) -> None:
    # Only finished downloads are old: one still running when the listener was added is delivered.
    seen = transfer.settled_guids(await files.run(transfer.downloads_steps(), cdp))
    frame_ids = await files.run(transfer.frame_ids_steps(), cdp)
    while listeners.handlers and not page.is_closed():
        entries = await files.run(transfer.downloads_steps(), cdp)
        unattributed = _only_listening_page(page)
        mine, others = transfer.frame_downloads(entries, seen, frame_ids, unattributed=unattributed)
        if any(item.get("frameId") for item in others):  # perhaps a frame the page gained since
            frame_ids = await files.run(transfer.frame_ids_steps(), cdp)
            mine, others = transfer.frame_downloads(entries, seen, frame_ids, unattributed=unattributed)
        seen.update(item.get("guid") for item in others)
        for info in mine:
            seen.add(info.guid)
            _deliver(listeners, StatelockDownload(**vars(info), page=page))
        await asyncio.sleep(transfer.DOWNLOAD_EVENT_POLL_INTERVAL)
