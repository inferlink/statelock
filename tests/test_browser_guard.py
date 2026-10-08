"""Page guard, input governance and refused commands, end to end."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

import websockets
from browser_support import (
    StatelockPolicyViolationError,
    actions,
    agent_key,
    network_log,
    run_session,
    running_server,
    wait_for_proxy_close,
)

pytestmark = pytest.mark.browser


def test_finance_match_passes_with_post_condition(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.click("text=Mark as Paid")
        return str(await page.inner_text("#status"))

    session_id, result = run_session(server, "finance_reconciliation_agent", "/demo/finance?scenario=match", steps)
    assert result == "Reconciliation complete"
    last = actions(server, session_id)[-1]
    assert last["post_verdict"]["decision"] == "allow"
    assert last["context"]["browser_state"]["target_selection"] == "action_session"


def test_finance_mismatch_is_blocked(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.click("text=Mark as Paid")
        return "clicked"

    _, result = run_session(server, "finance_reconciliation_agent", "/demo/finance?scenario=mismatch", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "assert_field_equal"
    assert "4,900.00" in result.reason


def test_enter_post_condition_failure(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.focus("button")
        await page.keyboard.press("Enter")
        await wait_for_proxy_close(page)
        return "pressed"

    _, result = run_session(server, "post_fail_agent", "/demo/finance?scenario=match", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.violation_type == "post_condition"


def test_agent_script_click_is_replayed_as_a_governed_click(server: dict[str, Any]) -> None:
    # Frameworks such as Stagehand click with element.click(): Statelock clicks for real instead.
    async def steps(page: Any) -> str:
        await page.evaluate("document.querySelector('button').click()")
        await page.wait_for_selector("text=Reconciliation complete", timeout=5000)
        return str(await page.inner_text("#status"))

    session_id, result = run_session(server, "finance_reconciliation_agent", "/demo/finance?scenario=match", steps)
    assert result == "Reconciliation complete"
    replayed = [
        r
        for r in actions(server, session_id)
        if "statelock_replayed_script_click" in r["context"]["params"]
        and "statelock_replay_not_sent" not in r["context"]["params"]  # the layout moved: located again
    ]
    assert [r["context"]["params"]["type"] for r in replayed] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert replayed[1]["context"]["browser_state"]["target_element"]["text"] == "Mark as Paid"
    assert replayed[-1]["post_verdict"]["decision"] == "allow"  # post-conditions run as for a real click


def test_replayed_script_click_is_checked_by_the_policy(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.evaluate("document.querySelector('button').click()")
        await wait_for_proxy_close(page)
        return str(await page.title())

    _, result = run_session(server, "finance_reconciliation_agent", "/demo/finance?scenario=mismatch", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "assert_field_equal"


@pytest.mark.parametrize(
    ("name", "steps_script"),
    [
        # The site's own element.click() (from a real click) is not agent code: still a violation.
        ("site_click", None),
        # A covered element: a real click would hit the overlay, so it is not replayed.
        ("covered", "document.getElementById('covered').click()"),
        (
            "dispatched_dblclick",
            "document.getElementById('hidden-go').dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))",
        ),
    ],
)
def test_synthetic_events_that_are_not_replayed_are_blocked(
    server: dict[str, Any], name: str, steps_script: str | None
) -> None:
    async def steps(page: Any) -> str:
        if steps_script is None:
            await page.click("#relay")
        else:
            await page.evaluate(steps_script)
        await wait_for_proxy_close(page)
        return str(await page.title())

    session_id, result = run_session(server, "flow_agent", "/gov/relay", steps)
    assert isinstance(result, StatelockPolicyViolationError), name
    assert result.rule == "synthetic_event"
    record = actions(server, session_id, method="Statelock.syntheticEvent")[-1]
    assert record["context"]["browser_state"]["title"] == "Relay"  # the site handler never ran


def test_absolute_guard_scope_matches_policy_origin_and_path(tmp_path_factory: pytest.TempPathFactory) -> None:
    policy = """
policies:
  - agent_id: lookalike_agent
    target_url_contains: http://localhost:__PORT__/gov/form
  - agent_id: trusted_agent
    target_url_contains: http://127.0.0.1:__PORT__/gov/form
"""

    async def paste_is_allowed(page: Any) -> bool:
        return bool(
            await page.evaluate(
                """() => {
                    const event = new Event('paste', {bubbles: true, cancelable: true});
                    document.body.dispatchEvent(event);
                    return !event.defaultPrevented;
                }"""
            )
        )

    with running_server(tmp_path_factory, policy=policy, agents=("lookalike_agent", "trusted_agent")) as scoped:
        port = scoped["base"].rsplit(":", 1)[1]

        async def lookalike(page: Any) -> bool:
            await page.goto(f"{scoped['base']}/http://localhost:{port}/gov/form")
            return await paste_is_allowed(page)

        _, result = run_session(scoped, "lookalike_agent", "/health", lookalike)
        assert result is True

        async def trusted(page: Any) -> None:
            await page.goto(scoped["base"] + "/gov/form-extra")
            assert await paste_is_allowed(page)
            await page.goto(scoped["base"] + "/gov/form")
            assert not await paste_is_allowed(page)
            await wait_for_proxy_close(page)
            await page.title()

        _, result = run_session(scoped, "trusted_agent", "/health", trusted)
        assert isinstance(result, StatelockPolicyViolationError)
        assert result.rule == "synthetic_event"


def test_multi_step_flow_is_allowed_and_attributed(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.fill("#q", "hello")
        await page.click("#go")
        await page.wait_for_url("**/gov/result")
        await page.click("#next")
        await page.wait_for_url("**/gov/step3")
        await page.go_back()
        await page.click("#later")
        await page.wait_for_url("**/gov/redirected", timeout=5000)
        return str(await page.title())

    session_id, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert result == "Redirected"
    log = network_log(server, session_id)
    assert all(entry["decision"] == "allow" for entry in log)
    by_url = {(entry["url"].split("/gov/")[1], entry["initiated_by"]) for entry in log}
    assert ("form", "agent_navigation") in by_url
    assert ("result", "site") in by_url
    assert ("api/ping", "site") in by_url
    assert ("redirected", "site") in by_url


@pytest.mark.parametrize(
    ("name", "script", "rule"),
    [
        ("fetch", "fetch('/gov/api/delete',{method:'POST'}).catch(()=>{})", "agent_code_request"),
        ("href", "location.href='/gov/step3'", "agent_code_request"),
        ("site_function", "app.save().catch(()=>{})", "agent_code_request"),
        ("form_submit", "document.getElementById('f').submit()", "untrusted_form_submission"),
        ("synthetic_drop", "document.body.dispatchEvent(new Event('drop', {bubbles: true}))", "synthetic_event"),
        (
            "inserted_script",
            "const s=document.createElement('script');"
            "s.textContent=\"fetch('/gov/api/x',{method:'POST'}).catch(()=>{})\";document.body.append(s)",
            "agent_code_request",
        ),
    ],
)
def test_agent_code_side_effects_are_blocked(server: dict[str, Any], name: str, script: str, rule: str) -> None:
    async def steps(page: Any) -> str:
        await page.evaluate(script)
        await wait_for_proxy_close(page)
        return str(await page.title())

    _, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert isinstance(result, StatelockPolicyViolationError), name
    assert result.rule == rule


def test_reload_script_is_attributed_to_agent_code(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> None:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.enable")
        await cdp.send("Page.reload", {"scriptToEvaluateOnLoad": "fetch('/gov/api/delete').catch(() => {})"})
        await wait_for_proxy_close(page)
        await page.title()

    _, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "agent_code_request"


def test_read_only_evaluate_is_allowed(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        return await page.evaluate("document.querySelectorAll('input').length")

    _, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert result == 1


def test_second_tab_is_captured(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.goto(server["base"] + "/health")
        second = await page.context.new_page()
        await second.goto(server["base"] + "/demo/finance?scenario=match")
        await second.click("text=Mark as Paid")
        return str(await second.inner_text("#status"))

    session_id, result = run_session(server, "finance_reconciliation_agent", "/health", steps)
    assert result == "Reconciliation complete"
    urls = {a["context"]["browser_state"]["url"] for a in actions(server, session_id)}
    assert all("/demo/finance" in url for url in urls)


def test_wrapped_command_is_refused(server: dict[str, Any]) -> None:
    async def raw() -> dict[str, Any]:
        headers = {"x-statelock-agent-id": "flow_agent", "authorization": f"Bearer {agent_key('flow_agent')}"}
        async with websockets.connect(server["ws"], additional_headers=headers) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Target.sendMessageToTarget", "params": {"message": "{}"}}))
            return dict(json.loads(await ws.recv()))

    response = asyncio.run(raw())
    assert "wrapped_command" in response["error"]["message"]


def test_agent_page_route_is_refused(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.route("**/*", lambda route: route.continue_())
        await page.goto(server["base"] + "/gov/result")
        return str(await page.title())

    _, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "agent_network_interception"


def test_drag_and_drop_is_governed(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.drag_and_drop("#item", "#zone")
        await page.wait_for_function("document.querySelector('#dropstatus').textContent === 'dropped'")
        return str(await page.inner_text("#dropstatus"))

    session_id, result = run_session(server, "flow_agent", "/gov/drag", steps)
    assert result == "dropped", result
    drags = [a for a in actions(server, session_id) if a["context"]["method"] == "Input.dispatchDragEvent"]
    assert {a["context"]["params"]["type"] for a in drags} >= {"dragEnter", "drop"}
    drop = next(a for a in drags if a["context"]["params"]["type"] == "drop")
    assert drop["context"]["action_kind"] == "drag"
    assert drop["context"]["post_browser_state"] is not None  # post-conditions ran after the drop


def test_agent_cannot_export_cookies_or_use_page_request(server: dict[str, Any]) -> None:
    # page.request reuses the browser's cookies from the agent's process, outside
    # Statelock. Reading cookies is declined, so it fails instead of bypassing.
    async def steps(page: Any) -> dict[str, str]:
        outcome: dict[str, str] = {}
        for name, call in (
            ("page_request", lambda: page.request.get(server["base"] + "/gov/api/secret")),
            ("cookies", page.context.cookies),
            ("storage_state", page.context.storage_state),
        ):
            try:
                await call()
                outcome[name] = "worked"
            except Exception as error:
                outcome[name] = "refused" if "cookies" in str(error) else f"other: {error}"
        outcome["title"] = str(await page.title())  # the session continues
        return outcome

    session_id, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert result == {"page_request": "refused", "cookies": "refused", "storage_state": "refused", "title": "Form"}
    declined = [r for r in actions(server, session_id) if r["verdict"].get("rule") == "cookie_export"]
    assert declined and all(r["context"]["method"] == "Storage.getCookies" for r in declined)


def test_cookie_access_returns_only_allowed_cookies(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> dict[str, Any]:
        cookies = await page.context.cookies()
        response = await page.request.get(server["base"] + "/gov/api/echo")  # works, without the session cookie
        return {"names": sorted(c["name"] for c in cookies), "status": response.status}

    session_id, result = run_session(server, "cookie_agent", "/gov/cookies", steps)
    assert result == {"names": ["csrftoken"], "status": 200}
    reads = [r for r in actions(server, session_id) if r["context"]["method"] == "Storage.getCookies"]
    assert reads and all(r["verdict"]["decision"] == "allow" for r in reads)
    evidence = reads[0]["verdict"]["evidence"]
    assert evidence["cookies_returned"] == ["csrftoken"] and evidence["cookies_withheld"] == 1
    assert "csrf-123" not in json.dumps(reads)  # names only, never values


def test_network_events_do_not_export_http_only_cookie(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> list[dict[str, Any]]:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Network.enable")
        seen: list[dict[str, Any]] = []

        def record(event: dict[str, Any]) -> None:
            seen.append(event)

        cdp.on("Network.requestWillBeSentExtraInfo", record)
        cdp.on("Network.responseReceivedExtraInfo", record)
        await page.goto(server["base"] + "/gov/form")
        await page.wait_for_load_state("networkidle")
        return seen

    _, result = run_session(server, "flow_agent", "/gov/cookies", steps)
    assert isinstance(result, list) and result
    assert "secret-session" not in json.dumps(result)
    assert "csrf-123" not in json.dumps(result)
