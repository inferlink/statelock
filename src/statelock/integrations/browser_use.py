# SPDX-License-Identifier: Apache-2.0
"""browser-use integration.

``browser-use`` accepts a Chrome DevTools Protocol URL through ``BrowserSession``.
Statelock's session URL is exactly that: a single-use governed browser endpoint.
browser-use then drives the governed Chromium over raw CDP, so every click and key
press goes through Statelock's checks (tests/test_browser_browser_use.py).

    from browser_use import Agent
    from statelock.integrations.browser_use import create_browser_use_session

    governed = await create_browser_use_session()       # STATELOCK_URL, STATELOCK_API_KEY
    agent = Agent(task="...", browser_session=governed.browser, llm=...)
    await agent.run()
    governed.raise_if_violation()                       # StatelockPolicyViolationError (rule, reason)

browser-use reports a blocked action to its LLM as a failed step, not as an
exception, so the agent may try other ways. Statelock blocks those too (the
session ends at the first violation); ``raise_if_violation`` turns the outcome into
the rule and reason after the run. A lookup Statelock refuses (a wrong key) raises
StatelockClientError rather than reporting "no violation".

Tested with browser-use 0.13 (Python 3.11+). browser-use sends anonymous usage
telemetry by default; set ``ANONYMIZED_TELEMETRY=false`` to turn it off.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from statelock.client.sessions import SessionUrl, create_session_url
from statelock.client.violations import StatelockPolicyViolationError

__all__ = ["BrowserUseSession", "create_browser_use_session"]


@dataclass
class BrowserUseSession:
    """A Statelock session plus the browser-use ``BrowserSession`` connected to it."""

    session: SessionUrl
    browser: Any  # browser_use.BrowserSession

    def violation(self, api_key: str | None = None) -> StatelockPolicyViolationError | None:
        """The violation that ended the session, or None (SessionUrl.violation: a failed
        lookup raises StatelockClientError). api_key defaults to the session's key."""
        return self.session.violation(api_key)

    def raise_if_violation(self, api_key: str | None = None) -> None:
        """Raise StatelockPolicyViolationError if Statelock ended the session."""
        violation = self.violation(api_key)
        if violation is not None:
            raise violation


async def create_browser_use_session(
    server_url: str | None = None,
    *,
    api_key: str | None = None,
    agent_id: str | None = None,
    ttl_seconds: int | None = None,
    saved_session: str | None = None,
    save_session: bool = False,
    **browser_session_kwargs: Any,
) -> BrowserUseSession:
    """Create a Statelock session URL and a ``browser_use.BrowserSession`` for it.

    ``browser_session_kwargs`` go to ``BrowserSession``. Needs Python 3.11+ and
    ``pip install "statelock-ai[browser-use]"``.
    """
    if sys.version_info < (3, 11):  # pragma: no cover - the extra installs nothing on 3.10
        raise RuntimeError("the browser-use integration needs Python 3.11+ (browser-use does)")
    try:
        from browser_use import BrowserSession  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - only without the optional dependency
        raise RuntimeError('browser-use is not installed; run: pip install "statelock-ai[browser-use]"') from error
    session = await asyncio.to_thread(  # a blocking HTTP request: off the event loop
        create_session_url,
        server_url,
        api_key=api_key,
        agent_id=agent_id,
        ttl_seconds=ttl_seconds,
        saved_session=saved_session,
        save_session=save_session,
    )
    return BrowserUseSession(session, BrowserSession(cdp_url=session.cdp_url, **browser_session_kwargs))
