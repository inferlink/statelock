# SPDX-License-Identifier: Apache-2.0
"""LangChain and CrewAI browser tools that stop when Statelock ends the session.

    from statelock.client import create_session_url
    from statelock.integrations.langchain import crewai_tools, governed_playwright_tools

    session = create_session_url()
    browser = await playwright.chromium.connect_over_cdp(session.cdp_url)
    tools = governed_playwright_tools(browser, session)   # LangChain's Playwright toolkit, wrapped
    # CrewAI: tools = crewai_tools(tools)

``statelock_tools(tools, session)`` wraps any other LangChain tools that drive the
governed browser.

Any LangChain browser tool works through Statelock once its browser comes from a
session URL: every click and keystroke is governed. The wrapper fixes one thing.
A tool that runs into a session Statelock ended fails with "browser closed", so
an agent would not learn that Statelock blocked the action and would keep trying.
When a wrapped tool fails, or its browser is no longer connected after it ran,
the wrapper asks Statelock whether the session ended in a violation and then
raises StatelockPolicyViolationError (rule, reason), which stops the agent run. A
violation found after an action that returned (a failed post-condition) is
raised by the next tool call, which runs into the closed session. Successful
calls cost no lookup. A lookup Statelock refuses (a wrong key) raises SessionUrlError.

The wrapper also raises LangChain's 1 s click timeout to 10 s (a governed click
waits for Statelock's checks), and turns off the click tool's ``visible_only``
selector suffix, which current Playwright never matches. It changes copies: the
tools passed in are left as they are.

Needs ``pip install "statelock-ai[langchain]"``; ``crewai_tools`` also needs ``crewai``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from playwright.async_api import Browser as AsyncBrowser
from playwright.sync_api import Browser as SyncBrowser

from statelock.client.sessions import SessionUrl

__all__ = ["crewai_tools", "governed_playwright_tools", "statelock_tools"]

# Governed clicks wait for Statelock's checks (and post-conditions); LangChain's
# Playwright tools default to a 1 s action timeout, which is too short for them.
MIN_ACTION_TIMEOUT_MS = 10_000


def statelock_tools(
    tools: Sequence[BaseTool],
    session: SessionUrl,
    *,
    api_key: str | None = None,
    action_timeout_ms: float = MIN_ACTION_TIMEOUT_MS,
) -> list[BaseTool]:
    """Wrapped copies of tools: a Statelock violation raises StatelockPolicyViolationError.

    Tools with a ``playwright_timeout`` shorter than action_timeout_ms get action_timeout_ms.
    api_key (for the violation lookup) defaults to the key the session was created with.
    """
    return [_governed(_adjusted(tool, action_timeout_ms), session, api_key) for tool in tools]


def _adjusted(tool: BaseTool, action_timeout_ms: float) -> BaseTool:
    changes: dict[str, Any] = {}
    timeout = getattr(tool, "playwright_timeout", None)
    if isinstance(timeout, (int, float)) and timeout < action_timeout_ms:
        changes["playwright_timeout"] = action_timeout_ms
    if getattr(tool, "visible_only", None) is True:
        # langchain-community 0.4 appends ">> visible=1", which current Playwright never
        # matches (it expects visible=true), so every click times out. Playwright's
        # click only acts on visible elements anyway.
        changes["visible_only"] = False
    return tool.model_copy(update=changes) if changes else tool


def _disconnected(tool: BaseTool) -> bool:
    """Whether the tool's governed browser (LangChain's Playwright tools have one) is gone."""
    browser = getattr(tool, "async_browser", None) or getattr(tool, "sync_browser", None)
    return isinstance(browser, (AsyncBrowser, SyncBrowser)) and not browser.is_connected()


def _governed(tool: BaseTool, session: SessionUrl, api_key: str | None) -> BaseTool:
    def check(error: BaseException | None) -> None:
        violation = session.violation(api_key)
        if violation is not None:
            raise violation from error

    def run(**kwargs: Any) -> Any:
        try:
            result = tool.run(kwargs)
        except Exception as error:
            check(error)
            raise
        if _disconnected(tool):
            check(None)
        return result

    async def arun(**kwargs: Any) -> Any:
        try:
            result = await tool.arun(kwargs)
        except Exception as error:
            await asyncio.to_thread(check, error)
            raise
        if _disconnected(tool):
            await asyncio.to_thread(check, None)
        return result

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def governed_playwright_tools(
    browser: AsyncBrowser | SyncBrowser,
    session: SessionUrl,
    *,
    api_key: str | None = None,
    action_timeout_ms: float = MIN_ACTION_TIMEOUT_MS,
) -> list[BaseTool]:
    """LangChain's Playwright toolkit for the governed ``browser``, wrapped with ``statelock_tools``.
    CrewAI drives tools synchronously: give it a sync Playwright browser."""
    from langchain_community.agent_toolkits import PlayWrightBrowserToolkit  # noqa: PLC0415

    if isinstance(browser, AsyncBrowser):
        toolkit = PlayWrightBrowserToolkit.from_browser(async_browser=browser)
    else:
        toolkit = PlayWrightBrowserToolkit.from_browser(sync_browser=browser)
    return statelock_tools(toolkit.get_tools(), session, api_key=api_key, action_timeout_ms=action_timeout_ms)


def crewai_tools(tools: Sequence[BaseTool]) -> list[Any]:
    """The wrapped LangChain tools as CrewAI tools (``crewai.tools.base_tool.Tool.from_langchain``)."""
    from crewai.tools.base_tool import Tool  # noqa: PLC0415

    return [Tool.from_langchain(tool) for tool in tools]
