"""Governed agent-side HTTP (Statelock.fetch, policy request_access) in a real browser."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from browser_support import API_TOKEN, StatelockPolicyViolationError, actions, network_log, run_session  # noqa: E402

import statelock.client  # noqa: E402
from statelock.audit.redaction import SECRET_MARKER  # noqa: E402
from statelock.client import StatelockRequestError, statelock_fetch  # noqa: E402

pytestmark = pytest.mark.browser
AGENT = "request_agent"


@pytest.fixture
def installed() -> Iterator[None]:
    statelock.client.install()
    yield
    statelock.client.uninstall()


def _fetches(server: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    return [r for r in actions(server, session_id) if r["context"]["method"] == "Statelock.fetch"]


def test_request_runs_in_the_page_with_the_browsers_session(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        await page.goto(server["base"] + "/gov/cookies")  # the site sets its HttpOnly session cookie
        response = await statelock_fetch(page, server["base"] + "/gov/me")
        return response.status, await response.json(), response.headers.get("content-type")

    session_id, (status, body, content_type) = run_session(server, AGENT, "/gov/form", steps)
    assert (status, body) == (200, {"user": "signed-in"})
    assert content_type == "application/json"
    record = _fetches(server, session_id)[0]
    assert record["verdict"]["decision"] == "allow"
    params = record["context"]["params"]
    assert params["status"] == 200
    assert len(params["response_sha256"]) == 64
    assert "secret-session" not in json.dumps(record)  # the cookie never reaches the evidence
    assert any(line.get("initiated_by") == "statelock_request" for line in network_log(server, session_id))


def test_playwrights_own_page_request_works_after_install(server: dict[str, Any], installed: None) -> None:
    async def steps(page: Any) -> Any:
        await page.goto(server["base"] + "/gov/cookies")
        me = await (await page.request.get(server["base"] + "/gov/me")).json()
        echoed = await (await page.request.post(server["base"] + "/gov/echo", data={"a": 1})).json()
        via_context = await page.context.request.get(server["base"] + "/gov/me")
        return me, echoed, via_context.ok

    _, (me, echoed, context_ok) = run_session(server, AGENT, "/gov/form", steps)
    assert me == {"user": "signed-in"}
    assert json.loads(echoed["body"]) == {"a": 1}
    assert echoed["content_type"] == "application/json"
    assert context_ok


def test_requests_the_policy_does_not_allow_are_declined(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> list[str]:
        errors = []
        for url, method in [("/gov/form", "GET"), ("/gov/me", "DELETE"), ("/gov/large", "GET")]:
            try:
                await statelock_fetch(page, server["base"] + url, method=method)
            except StatelockRequestError as error:
                errors.append(str(error))
        errors.append(await page.title())  # the session continues
        return errors

    session_id, errors = run_session(server, AGENT, "/gov/form", steps)
    assert "no request_access rule" in errors[0]
    assert "no request_access rule" in errors[1]
    assert "response_too_large" in errors[2]  # 300 kB, over the rule's max_response_bytes
    assert errors[3] == "Form"
    decisions = [(r["verdict"]["decision"], r["verdict"].get("rule")) for r in _fetches(server, session_id)]
    assert decisions[:2] == [("block", "request_access"), ("block", "request_access")]


def test_secret_placeholders_in_headers(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        response = await statelock_fetch(
            page, server["base"] + "/gov/echo", method="POST", headers={"X-Api-Token": "{{secret:api_token}}"}, data="x"
        )
        return await response.json()

    session_id, body = run_session(server, AGENT, "/gov/form", steps)
    assert body["token_ok"] is True  # the site received the real token
    record = _fetches(server, session_id)[0]
    assert record["context"]["params"]["statelock_secrets"] == ["api_token"]
    assert API_TOKEN not in json.dumps(actions(server, session_id))


def test_redirects_never_send_the_destination_request(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> list[Any]:
        await page.goto(server["base"] + "/gov/cookies")
        results: list[Any] = []
        for path in ("/gov/redirect-me", "/gov/redirect-away", "/gov/redirect-plain"):
            try:
                await statelock_fetch(page, server["base"] + path)
            except StatelockRequestError as error:
                results.append(str(error))
        return results

    session_id, results = run_session(server, AGENT, "/gov/form", steps)
    assert len(results) == 3 and all("fetch_failed" in error for error in results)
    assert not any(entry["url"].endswith("/gov/me") for entry in network_log(server, session_id))


def test_a_secret_header_is_removed_from_the_response(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        response = await statelock_fetch(
            page, server["base"] + "/gov/echo", method="POST", headers={"x-api-token": "{{secret:api_token}}"}, data="x"
        )
        return await response.text()

    session_id, text = run_session(server, AGENT, "/gov/form", steps)
    body = json.loads(text)
    assert body["token_ok"] is True
    assert body["token"] == SECRET_MARKER
    assert API_TOKEN not in text and API_TOKEN not in json.dumps(actions(server, session_id))


def test_a_malformed_request_is_declined_and_the_session_continues(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> list[str]:
        cdp = await page.context.new_cdp_session(page)
        errors = []
        for params in (
            {"url": server["base"] + "/gov/me", "body": "not base64!"},
            {"url": "ftp://x"},
            # Python reads the host after the "@"; the browser ends the host at the backslash.
            {"url": server["base"] + "\\@localhost/gov/me"},
        ):
            try:
                await cdp.send("Statelock.fetch", params)
            except Exception as error:
                errors.append(str(error))
        errors.append(await page.title())
        return errors

    _, errors = run_session(server, AGENT, "/gov/form", steps)
    assert "not valid base64" in errors[0] and "http(s) url" in errors[1] and "backslash" in errors[2]
    assert errors[3] == "Form"


def test_a_secret_placeholder_that_is_not_allowed_ends_the_session(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        await statelock_fetch(page, server["base"] + "/gov/me", headers={"x-api-token": "{{secret:api_token}}"})
        return await page.title()

    _, result = run_session(server, AGENT, "/gov/form", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "secret_injection"
