"""browser-use through Statelock: its CDP-driven actions are governed like any other client.

Drives browser-use's own BrowserSession events (no LLM): navigation, then a click the
finance policy blocks. Skipped without browser-use (``pip install "statelock-ai[browser-use]"``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
pytest.importorskip("browser_use")

from browser_support import actions, agent_key
from browser_use.browser.events import ClickElementEvent

from statelock.client import StatelockPolicyViolationError
from statelock.integrations.browser_use import BrowserUseSession, create_browser_use_session

pytestmark = pytest.mark.browser
AGENT = "finance_reconciliation_agent"


async def _click_mark_as_paid(server: dict[str, Any], scenario: str) -> tuple[BrowserUseSession, str | None]:
    """Open the finance demo in browser-use and click "Mark as Paid". Returns (session, click error)."""
    governed = await create_browser_use_session(server["base"], api_key=agent_key(AGENT))
    browser = governed.browser
    try:
        await browser.start()
        await browser.navigate_to(f"{server['base']}/demo/finance?scenario={scenario}")
        state = await browser.get_browser_state_summary()
        [index] = [i for i, el in state.dom_state.selector_map.items() if "Mark as Paid" in el.get_all_children_text()]
        node = await browser.get_element_by_index(index)
        event = browser.event_bus.dispatch(ClickElementEvent(node=node))
        try:
            await event
            await event.event_result(raise_if_any=True, raise_if_none=False)
        except Exception as error:
            return governed, str(error)
        await _released(server, governed.session.session_id)  # the record is written after the post-conditions
        return governed, None
    finally:
        await browser.kill()


async def _released(server: dict[str, Any], session_id: str) -> None:
    """Wait until the click's mouse release is recorded."""
    deadline = time.monotonic() + 10
    while not any(r["context"]["params"].get("type") == "mouseReleased" for r in actions(server, session_id)):
        if time.monotonic() > deadline:
            raise AssertionError("the click's mouse release was not recorded within 10 s")
        await asyncio.sleep(0.05)


def test_browser_use_clicks_are_governed(server: dict[str, Any]) -> None:
    governed, error = asyncio.run(_click_mark_as_paid(server, "mismatch"))
    assert error is not None and "STATELOCK_POLICY_VIOLATION" in error
    records = actions(server, governed.session.session_id)
    assert records and records[-1]["verdict"]["decision"] == "block"
    with pytest.raises(StatelockPolicyViolationError) as raised:
        governed.raise_if_violation()
    assert raised.value.rule == "assert_field_equal"


def test_browser_use_allowed_click_passes(server: dict[str, Any]) -> None:
    governed, error = asyncio.run(_click_mark_as_paid(server, "match"))
    assert error is None
    assert governed.violation() is None
    assert any(r["verdict"]["decision"] == "allow" for r in actions(server, governed.session.session_id))
