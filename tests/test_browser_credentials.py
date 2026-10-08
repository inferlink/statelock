"""Credential injection in a real browser: the agent types {{secret:name}}, the site gets the password."""

from __future__ import annotations

from typing import Any

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from browser_support import (  # noqa: E402
    PORTAL_PASSWORD,
    StatelockPolicyViolationError,
    actions,
    network_log,
    run_session,
)

pytestmark = pytest.mark.browser

PLACEHOLDER = "{{secret:portal_password}}"


def _evidence_text(server: dict[str, Any], session_id: str) -> str:
    session_dir = server["root"] / "artifacts" / "sessions" / session_id
    return "".join(
        path.read_text(errors="replace") for path in session_dir.rglob("*") if path.suffix in {".json", ".jsonl"}
    )


def test_placeholder_logs_in_without_the_agent_holding_the_password(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> dict[str, str]:
        await page.fill("#username", "editor")
        await page.fill("#password", PLACEHOLDER)
        read_back = await page.input_value("#password")  # the agent reading the field sees no secret
        evaluated = await page.evaluate("document.querySelector('#password').value")
        await page.click("#submit")
        await page.wait_for_load_state()
        return {"read_back": read_back, "evaluated": evaluated, "title": await page.title()}

    session_id, result = run_session(server, "secret_agent", "/gov/signin", steps)
    assert not isinstance(result, StatelockPolicyViolationError), result
    assert result["title"] == "Welcome"  # the site received the real password
    assert PORTAL_PASSWORD not in result["read_back"]
    assert PORTAL_PASSWORD not in result["evaluated"]
    assert result["evaluated"] == "[SECRET]"

    typed = [r for r in actions(server, session_id) if r["context"]["method"] == "Input.insertText"]
    secret_record = next(r for r in typed if r["context"]["params"].get("statelock_secrets"))
    assert secret_record["context"]["params"]["statelock_secrets"] == ["portal_password"]
    assert secret_record["context"]["params"]["text"] == "[SECRET]"
    assert PORTAL_PASSWORD not in _evidence_text(server, session_id)
    assert all(PORTAL_PASSWORD not in str(line) for line in network_log(server, session_id))


@pytest.mark.parametrize(
    ("agent", "start", "field", "expected"),
    [
        ("secret_agent", "/gov/signin", "#username", "may only be typed into a password field"),
        ("secret_agent", "/gov/form", "#q", "may only be typed on pages at http://127.0.0.1:"),
        ("flow_agent", "/gov/signin", "#password", "agent flow_agent may not use secret portal_password"),
    ],
)
def test_placeholder_where_it_is_not_allowed_ends_the_session(
    server: dict[str, Any], agent: str, start: str, field: str, expected: str
) -> None:
    async def steps(page: Any) -> None:
        await page.fill(field, PLACEHOLDER)

    session_id, result = run_session(server, agent, start, steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "secret_injection"
    assert expected in result.reason
    assert PORTAL_PASSWORD not in _evidence_text(server, session_id)
