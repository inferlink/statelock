"""An agent that clicks a button its policy prohibits. Plain Playwright through Statelock.

The default policy lets rogue_agent open the finance demo but not click "Mark as Paid"
(prohibit_click_text), so Statelock blocks the click and ends the session.

Environment: STATELOCK_URL (default http://localhost:8010; Docker: http://statelock:8000;
the proxy needs STATELOCK_DEMO=1), STATELOCK_API_KEY.
Exit code: 0 when Statelock blocked the click, 1 when it did not.
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

import statelock.client
from statelock.client import StatelockPolicyViolationError

DEFAULT_STATELOCK_URL = "http://localhost:8010"


async def run_rogue_agent() -> int:
    server = os.getenv("STATELOCK_URL") or DEFAULT_STATELOCK_URL
    async with (
        async_playwright() as playwright,
        await statelock.client.connect_playwright(playwright, server) as governed,
    ):
        try:
            async with governed.guard():
                await governed.page.goto(f"{server.rstrip('/')}/demo/finance?scenario=match")
                await governed.page.click("text=Mark as Paid")
                await governed.page.title()  # the next command sees a session Statelock ended
        except StatelockPolicyViolationError as violation:
            print("Statelock blocked the agent action.")
            print(violation)
            return 0
    print("NOT blocked: the prohibited click went through.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_rogue_agent()))
