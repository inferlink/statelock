"""Finance demo agent: plain Playwright through Statelock (the README's Quick start shape).

Environment:
  STATELOCK_URL           the Statelock server (default http://localhost:8010; Docker: http://statelock:8000)
  STATELOCK_API_KEY       the agent's key
  DEMO_FINANCE_SCENARIO   match | mismatch (default mismatch): the demo page STATELOCK_URL/demo/finance?scenario=...
  DEMO_EXPECT_BLOCK       true | false (default true)
"""

import asyncio
import os

from playwright.async_api import async_playwright

import statelock.client
from statelock.client import StatelockPolicyViolationError

DEFAULT_STATELOCK_URL = "http://localhost:8010"


async def run_finance_agent() -> None:
    server = os.getenv("STATELOCK_URL") or DEFAULT_STATELOCK_URL
    scenario = os.getenv("DEMO_FINANCE_SCENARIO", "mismatch")
    finance_demo_url = f"{server.rstrip('/')}/demo/finance?scenario={scenario}"
    expect_block = os.getenv("DEMO_EXPECT_BLOCK", "true").casefold() == "true"

    async with (
        async_playwright() as playwright,
        await statelock.client.connect_playwright(playwright, server) as governed,
    ):
        try:
            async with governed.guard():  # a violation raises StatelockPolicyViolationError
                await governed.page.goto(finance_demo_url)
                await governed.page.click("text=Mark as Paid")
                await governed.page.wait_for_selector("text=Reconciliation complete", timeout=10_000)
        except StatelockPolicyViolationError as violation:
            if not expect_block:
                raise
            print("Statelock blocked the finance reconciliation action.")
            print(violation)
            return
    print("Finance reconciliation action completed.")
    if expect_block:
        raise RuntimeError("Expected Statelock to block the action, but it was allowed.")


if __name__ == "__main__":
    asyncio.run(run_finance_agent())
