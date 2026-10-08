"""LangChain's Playwright browser tools (and CrewAI, which adopts LangChain tools) through Statelock.

Skipped without langchain-community (and, for the CrewAI test, crewai).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
pytest.importorskip("langchain_community")

from browser_support import agent_key
from langchain_community.agent_toolkits import PlayWrightBrowserToolkit
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from statelock.client import StatelockPolicyViolationError, create_session_url
from statelock.integrations.langchain import crewai_tools, governed_playwright_tools, statelock_tools

pytestmark = pytest.mark.browser
AGENT = "finance_reconciliation_agent"


async def _with_tools(server: dict[str, Any], steps: Any, *, wrap: bool = True) -> Any:
    session = create_session_url(server["base"], api_key=agent_key(AGENT))
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(session.cdp_url)
        try:
            tools = PlayWrightBrowserToolkit.from_browser(async_browser=browser).get_tools()
            if wrap:
                tools = governed_playwright_tools(browser, session, api_key=agent_key(AGENT))
            else:  # the same tool settings as statelock_tools, without the violation check
                for tool in tools:
                    if hasattr(tool, "visible_only"):
                        tool.visible_only = False
                        tool.playwright_timeout = 10_000
            return await steps({tool.name: tool for tool in tools})
        except StatelockPolicyViolationError as violation:
            return violation
        finally:
            if browser.is_connected():
                await browser.close()


def test_toolkit_works_through_statelock(server: dict[str, Any]) -> None:
    async def steps(tools: dict[str, Any]) -> tuple[str, str, str]:
        await tools["navigate_browser"].arun({"url": server["base"] + "/demo/finance?scenario=match"})
        text = await tools["extract_text"].arun({})
        clicked = await tools["click_element"].arun({"selector": "text=Mark as Paid"})
        return text, clicked, await tools["extract_text"].arun({})

    before, clicked, after = asyncio.run(_with_tools(server, steps))
    assert "Bank Deposit" in before
    assert clicked == "Clicked element 'text=Mark as Paid'"
    assert "Reconciliation complete" in after  # the post-condition passed


def test_blocked_click_stops_the_agent(server: dict[str, Any]) -> None:
    async def steps(tools: dict[str, Any]) -> str:
        await tools["navigate_browser"].arun({"url": server["base"] + "/demo/finance?scenario=mismatch"})
        return str(await tools["click_element"].arun({"selector": "text=Mark as Paid"}))

    # Unwrapped, the agent only learns that the browser closed, not why.
    with pytest.raises(PlaywrightError, match="has been closed"):
        asyncio.run(_with_tools(server, steps, wrap=False))
    violation = asyncio.run(_with_tools(server, steps))
    assert isinstance(violation, StatelockPolicyViolationError), violation
    assert violation.rule == "assert_field_equal"


def test_crewai_adopts_the_wrapped_tools(server: dict[str, Any]) -> None:
    """CrewAI calls tools synchronously, so it uses the toolkit with Playwright's sync API."""
    pytest.importorskip("crewai.tools.base_tool")
    session = create_session_url(server["base"], api_key=agent_key(AGENT))
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(session.cdp_url)
        try:
            tools = governed_playwright_tools(browser, session, api_key=agent_key(AGENT))
            crew = {tool.name: tool for tool in crewai_tools(tools)}
            crew["navigate_browser"].run(url=server["base"] + "/demo/finance?scenario=mismatch")
            with pytest.raises(StatelockPolicyViolationError) as raised:
                crew["click_element"].run(selector="text=Mark as Paid")
        finally:
            if browser.is_connected():
                browser.close()
    assert raised.value.rule == "assert_field_equal"


def test_wrapping_leaves_the_callers_tools_alone(server: dict[str, Any]) -> None:
    async def steps() -> tuple[list[Any], list[Any]]:
        session = create_session_url(server["base"], api_key=agent_key(AGENT))
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(session.cdp_url)
            try:
                tools = PlayWrightBrowserToolkit.from_browser(async_browser=browser).get_tools()
                statelock_tools(tools, session)
                return tools, PlayWrightBrowserToolkit.from_browser(async_browser=browser).get_tools()
            finally:
                await browser.close()

    wrapped, fresh = asyncio.run(steps())
    settings = [(getattr(t, "visible_only", None), getattr(t, "playwright_timeout", None)) for t in wrapped]
    assert settings == [(getattr(t, "visible_only", None), getattr(t, "playwright_timeout", None)) for t in fresh]
