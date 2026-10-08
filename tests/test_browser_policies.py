"""Remembered fields across pages, thresholds, upload rules and secrets, end to end."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from browser_support import StatelockPolicyViolationError, actions, portal_session, run_session

pytestmark = pytest.mark.browser


def test_portal_match_remembers_deposit_and_completes(server: dict[str, Any]) -> None:
    session_id, result = asyncio.run(portal_session(server, "match"))
    assert result == "completed", result
    records = actions(server, session_id)
    remembered = records[0]["context"]["remembered"]["bank_deposit"]
    assert remembered["value"] == "$5,000.00"
    assert "/demo/bank" in remembered["url"]
    assert remembered["source"] in {"page_load", "before_navigation"}
    upload = next(r for r in records if r["context"]["method"] == "DOM.setFileInputFiles")
    assert upload["verdict"]["decision"] == "allow"
    assert records[-1]["post_verdict"]["decision"] == "allow"


def test_portal_mismatch_is_blocked_across_pages(server: dict[str, Any]) -> None:
    _, result = asyncio.run(portal_session(server, "mismatch"))
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "assert_field_equal"
    assert "4,900.00" in result.reason
    assert "5,000.00" in result.reason


def test_portal_large_invoice_hits_threshold(server: dict[str, Any]) -> None:
    _, result = asyncio.run(portal_session(server, "large"))
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "assert_compare"
    assert "25,000.00" in result.reason


def test_portal_wrong_upload_type_is_blocked(server: dict[str, Any]) -> None:
    _, result = asyncio.run(portal_session(server, "match", upload_name="remittance.exe"))
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "restrict_uploads"


def test_secrets_typed_into_password_fields_stay_out_of_the_evidence(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.fill("#user", "alice")
        await page.fill("#pw", "hunter2-fill")
        await page.click("#pw")
        await page.keyboard.type("Zq9")
        await page.fill("#otp", "424242")
        return str(await page.input_value("#pw"))

    session_id, result = run_session(server, "flow_agent", "/gov/login", steps)
    assert result == "hunter2-fillZq9"  # the page got the real input
    session_dir = server["root"] / "artifacts" / "sessions" / session_id
    evidence = "\n".join(path.read_text() for path in session_dir.rglob("*.json*"))
    for secret in ("hunter2", "Zq9", '"Z"', '"q"', "424242"):
        assert secret not in evidence, secret
    assert "alice" in evidence  # ordinary fields are still recorded
    assert "secret_input" in evidence
