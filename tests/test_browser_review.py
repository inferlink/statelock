"""Human review (on_fail: review) end to end: pause, approve or deny, evidence."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

import httpx
from browser_support import (
    REVIEWER_KEY,
    WAIT_MS,
    StatelockPolicyViolationError,
    actions,
    agent_key,
    agent_session,
    site_calls,
)

from statelock.audit.records import SCHEMA_VERSION

pytestmark = pytest.mark.browser

REVIEWER = {"Authorization": f"Bearer {REVIEWER_KEY}"}


async def _decide(
    server: dict[str, Any], decision: str, before: Callable[[], Awaitable[Any]] | None = None
) -> dict[str, Any]:
    """Wait for the first pending review (and ``before``, if given), then approve or deny it."""
    async with httpx.AsyncClient(base_url=server["base"], headers=REVIEWER) as http:
        for _ in range(200):
            reviews = (await http.get("/reviews")).json()["reviews"]
            if reviews:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("no review was requested")
        review = reviews[0]
        if before is not None:
            await before()
        response = await http.post(
            f"/reviews/{review['review_id']}/decision", json={"decision": decision, "comment": "checked the invoice"}
        )
        assert response.status_code == 200, response.text
        return review


def _session(
    server: dict[str, Any],
    selector: str,
    decision: str | None,
    *,
    start: str = "/gov/approve",
    decide_after: str | None = None,
) -> tuple[str, Any, dict[str, Any] | None]:
    """``decide_after``: a test-site API call (/gov/api/<name>) the reviewer waits for before deciding."""
    seen: dict[str, Any] = {}

    async def site_called() -> None:
        deadline = time.monotonic() + WAIT_MS / 1000
        while decide_after not in site_calls(server["app"]):
            assert time.monotonic() < deadline, f"the page did not call /gov/api/{decide_after}"
            await asyncio.sleep(0.05)

    async def steps(page: Any) -> str:
        before = site_called if decide_after else None
        reviewer = asyncio.ensure_future(_decide(server, decision, before)) if decision else None
        try:
            # A reviewed action waits for a person: the agent's timeout must allow for that.
            await page.click(selector, timeout=60_000)
            await page.wait_for_selector("text=paid", timeout=WAIT_MS)
            return "paid"
        finally:
            if reviewer is not None:
                seen["review"] = await reviewer

    async def run() -> tuple[str, Any]:
        return await agent_session(server, "review_agent", start, steps)

    session_id, result = asyncio.run(run())
    return session_id, result, seen.get("review")


def test_approved_click_proceeds_with_review_evidence(server: dict[str, Any]) -> None:
    session_id, result, review = _session(server, "#pay", "approve")
    assert result == "paid", result
    assert review is not None
    assert review["action"]["description"] == 'click "Pay"'
    assert review["failures"][0]["rule"] == "assert_compare"
    assert review["has_screenshot"]

    records = actions(server, session_id)
    pressed = next(r for r in records if r["context"]["params"].get("type") == "mousePressed")
    released = next(r for r in records if r["context"]["params"].get("type") == "mouseReleased")
    assert pressed["verdict"]["decision"] == "allow"
    assert pressed["review"]["status"] == "approved"
    assert pressed["review"]["reviewer_id"] == "test_reviewer"
    assert pressed["review"]["comment"] == "checked the invoice"
    assert pressed["review"]["screenshot_file"] == "review-screenshot.jpg"
    # The approved press covers its release: one review for the whole click.
    assert released["review"]["review_id"] == pressed["review"]["review_id"]
    moved = [r for r in records if r["context"]["params"].get("type") == "mouseMoved"]
    assert moved and all(r["verdict"]["evidence"]["review_deferred"] for r in moved)
    assert moved[0]["review"] is None
    assert all(r["schema_version"] == SCHEMA_VERSION for r in records)

    listed = httpx.get(f"{server['base']}/reviews?status=all", headers=REVIEWER).json()["reviews"]
    assert [r["review_id"] for r in listed if r["session_id"] == session_id] == [review["review_id"]]


def test_denied_click_ends_the_session(server: dict[str, Any]) -> None:
    session_id, result, _ = _session(server, "#pay", "deny")
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "assert_compare"
    assert "Denied by reviewer test_reviewer" in result.reason
    violation = httpx.get(
        f"{server['base']}/violations/{session_id}", headers={"Authorization": f"Bearer {agent_key('review_agent')}"}
    ).json()
    assert violation["review"]["status"] == "denied"
    pressed = next(r for r in actions(server, session_id) if r["context"]["params"].get("type") == "mousePressed")
    assert pressed["verdict"]["decision"] == "block"
    assert pressed["review"]["status"] == "denied"


def test_blocking_rule_wins_without_review(server: dict[str, Any]) -> None:
    session_id, result, _ = _session(server, "#del", None)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
    listed = httpx.get(f"{server['base']}/reviews?status=all", headers=REVIEWER).json()["reviews"]
    assert not [r for r in listed if r["session_id"] == session_id]


def test_page_change_during_review_blocks(server: dict[str, Any]) -> None:
    # The amount changes from $5,000 to $9,000 while the reviewer looks at $5,000.
    _, result, review = _session(server, "#pay", "approve", start="/gov/approve?drift=1", decide_after="drifted")
    assert review is not None and "5,000.00" in review["failures"][0]["reason"]
    assert isinstance(result, StatelockPolicyViolationError), result
    assert "page changed while the action waited" in result.reason
    assert "9,000.00" in result.reason


def test_agent_key_cannot_review(server: dict[str, Any]) -> None:
    response = httpx.get(f"{server['base']}/reviews", headers={"Authorization": f"Bearer {agent_key('review_agent')}"})
    assert response.status_code == 401
