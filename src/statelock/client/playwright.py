# SPDX-License-Identifier: Apache-2.0
"""Small Playwright wrappers for the common Statelock integration path.

The low-level pieces remain available: ``create_session_url()``,
``connect_over_cdp()``, and ``statelock.client.install()``. This module keeps the
starter path short and explicit for existing Playwright scripts:

    import statelock.client

    statelock.client.install()
    async with await statelock.client.connect_playwright(playwright) as governed:
        async with governed.guard():
            await governed.page.goto(...)

For sync Playwright, the same names end in ``_sync`` / ``Sync``:

    statelock.client.install_sync()
    with statelock.client.connect_playwright_sync(playwright) as governed, governed.guard():
        governed.page.goto(...)
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import Page as AsyncPage
from playwright.async_api import Playwright as AsyncPlaywright
from playwright.sync_api import Browser as SyncBrowser
from playwright.sync_api import Page as SyncPage
from playwright.sync_api import Playwright as SyncPlaywright

from statelock.client.sessions import SessionUrl, create_session_url


@dataclass
class PlaywrightSession:
    """A Statelock session plus the async Playwright browser connected to it.
    ``async with`` closes the browser connection at the end."""

    session: SessionUrl
    browser: AsyncBrowser

    @property
    def page(self) -> AsyncPage:
        """The first page in the governed browser context."""
        return self.browser.contexts[0].pages[0]

    def guard(self, api_key: str | None = None) -> AbstractAsyncContextManager[None]:
        """Raise StatelockPolicyViolationError when Statelock blocks the session.
        api_key defaults to the key the session was created with."""
        return self.session.guard(api_key)

    async def close(self) -> None:
        """Close the Playwright browser connection."""
        await self.browser.close()

    async def __aenter__(self) -> PlaywrightSession:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()


@dataclass
class PlaywrightSessionSync:
    """A Statelock session plus the sync Playwright browser connected to it.
    ``with`` closes the browser connection at the end."""

    session: SessionUrl
    browser: SyncBrowser

    @property
    def page(self) -> SyncPage:
        """The first page in the governed browser context."""
        return self.browser.contexts[0].pages[0]

    def guard(self, api_key: str | None = None) -> AbstractContextManager[None]:
        """Raise StatelockPolicyViolationError when Statelock blocks the session.
        api_key defaults to the key the session was created with."""
        return self.session.guard_sync(api_key)

    def close(self) -> None:
        """Close the Playwright browser connection."""
        self.browser.close()

    def __enter__(self) -> PlaywrightSessionSync:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


async def connect_playwright(
    playwright: AsyncPlaywright,
    server_url: str | None = None,
    *,
    api_key: str | None = None,
    agent_id: str | None = None,
    ttl_seconds: int | None = None,
    saved_session: str | None = None,
    save_session: bool = False,
    **connect_kwargs: Any,
) -> PlaywrightSession:
    """Create a Statelock session URL and connect async Playwright over CDP.

    server_url defaults to STATELOCK_URL, api_key to STATELOCK_API_KEY (see create_session_url).
    """
    session = await asyncio.to_thread(  # a blocking HTTP request: off the event loop
        create_session_url,
        server_url,
        api_key=api_key,
        agent_id=agent_id,
        ttl_seconds=ttl_seconds,
        saved_session=saved_session,
        save_session=save_session,
    )
    browser = await playwright.chromium.connect_over_cdp(session.cdp_url, **connect_kwargs)
    return PlaywrightSession(session=session, browser=browser)


def connect_playwright_sync(
    playwright: SyncPlaywright,
    server_url: str | None = None,
    *,
    api_key: str | None = None,
    agent_id: str | None = None,
    ttl_seconds: int | None = None,
    saved_session: str | None = None,
    save_session: bool = False,
    **connect_kwargs: Any,
) -> PlaywrightSessionSync:
    """``connect_playwright`` for Playwright's sync API."""
    session = create_session_url(
        server_url,
        api_key=api_key,
        agent_id=agent_id,
        ttl_seconds=ttl_seconds,
        saved_session=saved_session,
        save_session=save_session,
    )
    browser = playwright.chromium.connect_over_cdp(session.cdp_url, **connect_kwargs)
    return PlaywrightSessionSync(session=session, browser=browser)
