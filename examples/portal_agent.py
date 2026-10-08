"""Two-page portal agent: read a deposit in the bank portal, then settle the invoice in the ERP.

Statelock remembers the deposit from the bank page (policy field with
remember: true) and, on the ERP page, checks it against the invoice, checks the
invoice against a threshold, and restricts the remittance upload. Invoices over
the threshold (scenario large) wait for a human reviewer: approve or deny them at
http://localhost:8010/review.

Environment:
  STATELOCK_URL          the Statelock server (default http://localhost:8010; Docker: http://statelock:8000)
  STATELOCK_API_KEY      the agent's key
  DEMO_PORTAL_BASE       where the demo pages are (default: STATELOCK_URL)
  DEMO_PORTAL_SCENARIO   match | mismatch | large
  DEMO_UPLOAD_NAME       remittance.pdf (use e.g. remittance.exe to see the upload rule block)
  DEMO_EXPECT_BLOCK      true | false
  DEMO_ACTION_TIMEOUT    seconds an action may take, including a wait for review (default 360)
"""

import asyncio
import os

from playwright.async_api import async_playwright

import statelock.client
from statelock.client import StatelockPolicyViolationError

DEFAULT_STATELOCK_URL = "http://localhost:8010"


async def run_portal_agent() -> None:
    server = os.getenv("STATELOCK_URL") or DEFAULT_STATELOCK_URL
    base = (os.getenv("DEMO_PORTAL_BASE") or server).rstrip("/")
    scenario = os.getenv("DEMO_PORTAL_SCENARIO", "match")
    upload_name = os.getenv("DEMO_UPLOAD_NAME", "remittance.pdf")
    expect_block = os.getenv("DEMO_EXPECT_BLOCK", "false").casefold() == "true"
    # An action paused for review answers only when a reviewer decides: Playwright's
    # default 30 s action timeout would give up first.
    action_timeout_ms = float(os.getenv("DEMO_ACTION_TIMEOUT", "360")) * 1000

    statelock.client.install()  # Playwright's own set_input_files goes through Statelock
    async with (
        async_playwright() as playwright,
        await statelock.client.connect_playwright(playwright, server) as governed,
    ):
        page = governed.page
        page.context.set_default_timeout(action_timeout_ms)
        try:
            async with governed.guard():
                await page.goto(f"{base}/demo/bank?scenario={scenario}")
                deposit = await page.inner_text("#deposit-amount")
                print(f"Bank deposit: {deposit}")

                await page.goto(f"{base}/demo/erp?scenario={scenario}")
                remittance = {"name": upload_name, "mimeType": "application/pdf", "buffer": b"%PDF-1.4 demo\n"}
                print("Uploading the remittance (large invoices wait for a reviewer at /review)")
                await page.set_input_files("#remittance", [remittance])
                await page.click("text=Mark as Paid")
                await page.wait_for_selector("text=Reconciliation complete", timeout=10_000)
        except StatelockPolicyViolationError as violation:
            if not expect_block:
                raise
            print("Statelock blocked the portal reconciliation.")
            print(violation)
            return
    print("Portal reconciliation completed.")
    if expect_block:
        raise RuntimeError("Expected Statelock to block the action, but it was allowed.")


if __name__ == "__main__":
    asyncio.run(run_portal_agent())
