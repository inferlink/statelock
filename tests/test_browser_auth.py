"""Agent authentication, end to end."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

import httpx
from browser_support import (
    StatelockPolicyViolationError,
    actions,
    agent_key,
    connect_with_headers,
    playwright_api,
    run_session,
)

from statelock.wire import CLOSE_CODE_UNREGISTERED_AGENT

pytestmark = pytest.mark.browser


def test_playwright_with_a_wrong_key_cannot_connect(server: dict[str, Any]) -> None:
    async def attempt() -> BaseException | None:
        async with playwright_api.async_playwright() as p:
            try:
                conn = await connect_with_headers(p, server["ws"], "flow_agent", api_key="slk_wrong")
            except Exception as error:
                return error
            await conn.browser.close()
            return None

    error = asyncio.run(attempt())
    assert error is not None
    assert f"code={CLOSE_CODE_UNREGISTERED_AGENT}" in str(error)  # refused by authentication, not just any failure


def test_violation_details_need_the_agents_key(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.click("text=Mark as Paid")
        return "clicked"

    session_id, result = run_session(server, "finance_reconciliation_agent", "/demo/finance?scenario=mismatch", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.violation["tenant_id"] == "test"
    url = f"{server['base']}/violations/{session_id}"
    assert httpx.get(url).status_code == 401
    other = {"authorization": f"Bearer {agent_key('flow_agent')}"}
    assert httpx.get(url, headers=other).status_code == 404
    own = {"authorization": f"Bearer {agent_key('finance_reconciliation_agent')}"}
    assert httpx.get(url, headers=own).json()["rule"] == "assert_field_equal"
    record = actions(server, session_id)[-1]
    assert record["context"]["tenant_id"] == "test"
