"""Touch input through Statelock: a Playwright tap is governed like a click."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from browser_support import StatelockPolicyViolationError, actions, agent_key, running_server
from playwright.async_api import async_playwright

from statelock.client import connect_statelock

pytestmark = pytest.mark.browser

POLICY = """
policies:
  - agent_id: tap_agent
    target_url_contains: /demo/finance
    pre_conditions:
      - assert_field_equal: {left: bank_deposit_amount, right: erp_invoice_amount}
    post_conditions:
      - trigger: {click_text: [Mark as Paid]}
        require_page_text: {values: [Reconciliation complete]}
  - agent_id: tap_block_agent
    target_url_contains: /demo/finance
    pre_conditions:
      - prohibit_click_text: {values: [Mark as Paid]}
"""


@pytest.fixture(scope="module")
def touch_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    with running_server(tmp_path_factory, policy=POLICY, agents=("tap_agent", "tap_block_agent")) as running:
        yield running


def _tap(server: dict[str, Any], agent: str) -> tuple[str, Any]:
    async def session() -> tuple[str, Any]:
        async with async_playwright() as playwright:
            conn = await connect_statelock(playwright, server["ws"], agent, api_key=agent_key(agent))
            try:
                async with conn.guard():
                    context = await conn.browser.new_context(has_touch=True)
                    page = await context.new_page()
                    await page.goto(server["base"] + "/demo/finance?scenario=match", timeout=90_000)
                    await page.tap("text=Mark as Paid")
                    return conn.session_id, await page.inner_text("#status")
            except StatelockPolicyViolationError as violation:
                return conn.session_id, violation
            finally:
                if conn.browser.is_connected():
                    await conn.browser.close()

    return asyncio.run(session())


def _touches(server: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    return [r for r in actions(server, session_id) if r["context"]["method"] == "Input.dispatchTouchEvent"]


def test_tap_runs_post_conditions_on_the_tapped_element(touch_server: dict[str, Any]) -> None:
    session_id, result = _tap(touch_server, "tap_agent")
    assert result == "Reconciliation complete", result
    touches = {r["context"]["params"]["type"]: r for r in _touches(touch_server, session_id)}
    assert touches["touchStart"]["context"]["browser_state"]["target_element"]["text"] == "Mark as Paid"
    end = touches["touchEnd"]
    # touchEnd carries no points: the element is the one the touch started on.
    assert end["context"]["browser_state"]["target_element"]["text"] == "Mark as Paid"
    assert end["post_verdict"]["decision"] == "allow"
    assert end["context"]["post_browser_state"] is not None


def test_tap_on_a_prohibited_element_is_blocked(touch_server: dict[str, Any]) -> None:
    session_id, result = _tap(touch_server, "tap_block_agent")
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
    blocked = _touches(touch_server, session_id)[-1]
    assert blocked["context"]["params"]["type"] == "touchStart"
    assert blocked["verdict"]["decision"] == "block"


def test_char_enter_on_focused_button_is_governed(touch_server: dict[str, Any]) -> None:
    async def session() -> tuple[str, Any]:
        async with async_playwright() as playwright:
            conn = await connect_statelock(
                playwright, touch_server["ws"], "tap_block_agent", api_key=agent_key("tap_block_agent")
            )
            try:
                async with conn.guard():
                    page = await conn.browser.new_page()
                    await page.goto(touch_server["base"] + "/demo/finance?scenario=match")
                    await page.locator("button").focus()
                    cdp = await page.context.new_cdp_session(page)
                    await cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": "\r"})
            except StatelockPolicyViolationError as violation:
                return conn.session_id, violation
            finally:
                if conn.browser.is_connected():
                    await conn.browser.close()
            return conn.session_id, None

    session_id, result = asyncio.run(session())
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
    assert actions(touch_server, session_id)[-1]["context"]["params"]["type"] == "char"


def test_nested_button_label_with_nbsp_is_blocked(touch_server: dict[str, Any]) -> None:
    async def session() -> Any:
        async with async_playwright() as playwright:
            conn = await connect_statelock(
                playwright, touch_server["ws"], "tap_block_agent", api_key=agent_key("tap_block_agent")
            )
            try:
                async with conn.guard():
                    page = await conn.browser.new_page()
                    await page.goto(touch_server["base"] + "/demo/finance?scenario=match")
                    await page.locator("button").evaluate("el => el.innerHTML = '<span>Mark&nbsp;as<br>Paid</span>'")
                    await page.locator("button span").click()
            except StatelockPolicyViolationError as violation:
                return violation
            finally:
                if conn.browser.is_connected():
                    await conn.browser.close()
            return None

    result = asyncio.run(session())
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
