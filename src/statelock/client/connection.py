# SPDX-License-Identifier: Apache-2.0
"""Connecting a Playwright agent to Statelock with headers (instead of a session URL)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, Locator, Page, Playwright

from statelock.client import files, transfer
from statelock.client.sessions import resolve_api_key
from statelock.client.transfer import UploadFile
from statelock.client.violations import StatelockPolicyViolationError, complete_violation, statelock_guard
from statelock.wire import AGENT_ID_HEADER, SESSION_ID_HEADER

DOWNLOAD_TIMEOUT = 30.0


@dataclass
class StatelockConnection:
    """One Statelock session: the agent's identity, and the browser once connected.

    ``connect_statelock`` creates and connects one. An agent framework that opens its
    own CDP connection (for example Stagehand's ``cdp_url``) creates one directly,
    passes ``headers`` to that connection, then calls ``attach`` with the browser:

        conn = StatelockConnection("ws://localhost:8010/statelock", "my_agent")
        browser = await playwright.chromium.connect_over_cdp(conn.endpoint_url, headers=conn.headers)
        conn.attach(browser)
    """

    endpoint_url: str
    agent_id: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Defaults to the STATELOCK_API_KEY environment variable; "" sends no key.
    api_key: str | None = field(default=None, repr=False)
    _browser: Browser | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.api_key = resolve_api_key(self.api_key)

    @property
    def headers(self) -> dict[str, str]:
        """WebSocket headers that authenticate and identify this session."""
        headers = {AGENT_ID_HEADER: self.agent_id, SESSION_ID_HEADER: self.session_id}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("StatelockConnection is not connected: call attach() or use connect_statelock()")
        return self._browser

    def attach(self, browser: Browser) -> None:
        """Record the browser connected with ``headers`` (by this SDK or an agent framework)."""
        self._browser = browser

    def guard(self) -> AbstractAsyncContextManager[None]:
        """Context manager that raises StatelockPolicyViolationError for violations."""
        return statelock_guard(self.endpoint_url, self.session_id, self.api_key or "")

    async def set_input_files(
        self,
        page: Page,
        target: str | Locator,
        files_to_upload: UploadFile | Sequence[UploadFile],
    ) -> list[dict[str, Any]]:
        """Upload files to Statelock and put them into a file input, as a governed action.

        ``target`` is a Playwright selector or Locator for an ``<input type=file>``
        in the page's main frame. Returns what Statelock stored for each file
        (path, name, size, sha256, mimeType); the same data is in the action record.
        """
        return await files.set_input_files(page, target, files_to_upload)

    @asynccontextmanager
    async def expect_download(
        self, page: Page, timeout: float = DOWNLOAD_TIMEOUT
    ) -> AsyncIterator[files.DownloadWaiter]:
        """Wait for the next download the block starts, after Statelock has checked it.

        Raises StatelockPolicyViolationError if Statelock blocked the download.
        """
        async with files.expect_download(page, timeout, self._blocked) as waiter:
            yield waiter

    async def _blocked(self, blocked: transfer.DownloadBlocked) -> BaseException:
        error = StatelockPolicyViolationError(transfer.blocked_violation(blocked, self.session_id))
        return await asyncio.to_thread(
            complete_violation, error, self.endpoint_url, self.session_id, self.api_key or ""
        )


async def connect_statelock(
    playwright: Playwright,
    endpoint_url: str,
    agent_id: str,
    session_id: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> StatelockConnection:
    """Connect Playwright to Statelock, authenticate, and identify the agent and session.

    api_key defaults to the STATELOCK_API_KEY environment variable. Without a key
    the proxy accepts the connection only when its authentication is off.
    """
    identity = {"session_id": session_id} if session_id is not None else {}
    conn = StatelockConnection(endpoint_url, agent_id, api_key=api_key, **identity)
    headers = {**(kwargs.pop("headers", None) or {}), **conn.headers}
    conn.attach(await playwright.chromium.connect_over_cdp(endpoint_url, headers=headers, **kwargs))
    return conn
