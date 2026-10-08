# SPDX-License-Identifier: Apache-2.0
"""Connecting with WebSocket headers instead of a session URL (not part of the public API).

Agents use a session URL (``connect_playwright``, ``create_session_url``). An agent
that can send headers may instead connect to ``ws://.../statelock`` with
``Authorization: Bearer <key>`` and the agent and session id headers; the test
suite does this to choose its session ids.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, Page, Playwright

from statelock.client.files import FileMethods
from statelock.client.sessions import resolve_api_key
from statelock.client.violations import statelock_guard
from statelock.wire import AGENT_ID_HEADER, SESSION_ID_HEADER


@dataclass
class HeaderConnection(FileMethods):
    """One Statelock session opened with headers: the agent's identity, and the
    browser once connected (``attach``)."""

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
            raise RuntimeError("HeaderConnection is not connected: call attach() or use connect_with_headers()")
        return self._browser

    @property
    def page(self) -> Page:
        """The first page in the governed browser context."""
        return self.browser.contexts[0].pages[0]

    def attach(self, browser: Browser) -> None:
        """Record the browser connected with ``headers``."""
        self._browser = browser

    def guard(self, api_key: str | None = None) -> AbstractAsyncContextManager[None]:
        """Raise StatelockPolicyViolationError when Statelock blocks the session.
        api_key defaults to the connection's key."""
        return statelock_guard(self.endpoint_url, self.session_id, resolve_api_key(api_key, self.api_key))


async def connect_with_headers(
    playwright: Playwright,
    endpoint_url: str,
    agent_id: str,
    session_id: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> HeaderConnection:
    """Connect Playwright to ``endpoint_url`` (ws://.../statelock) with the identity headers.

    api_key defaults to the STATELOCK_API_KEY environment variable. Without a key
    the proxy accepts the connection only when its authentication is off.
    """
    identity = {"session_id": session_id} if session_id is not None else {}
    conn = HeaderConnection(endpoint_url, agent_id, api_key=api_key, **identity)
    headers = {**(kwargs.pop("headers", None) or {}), **conn.headers}
    conn.attach(await playwright.chromium.connect_over_cdp(endpoint_url, headers=headers, **kwargs))
    return conn
