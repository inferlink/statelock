"""Statelock's own isolated worlds, local URLs and script-click replay, end to end."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from browser_support import StatelockPolicyViolationError, actions, run_session
from playwright.async_api import async_playwright

from statelock.proxy.js import load_js

pytestmark = pytest.mark.browser

FINANCE = "/demo/finance?scenario=match"


async def _statelock_contexts(page: Any) -> tuple[Any, dict[str, dict[str, Any]]]:
    """The agent's CDP session on the page, and the Statelock worlds the browser announced to it."""
    cdp = await page.context.new_cdp_session(page)
    seen: dict[str, dict[str, Any]] = {}
    cdp.on("Runtime.executionContextCreated", lambda event: seen.setdefault(event["context"]["name"], event["context"]))
    await cdp.send("Runtime.enable")
    await page.mouse.move(5, 5)  # a governed action: Statelock's state world exists now
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not {"", "__statelock_guard", "__statelock_state"} <= seen.keys():
        assert loop.time() < deadline, f"Statelock worlds not announced: {sorted(seen)}"
        await asyncio.sleep(0.05)
    return cdp, seen


@pytest.mark.parametrize(
    "attempt",
    [
        "evaluate_in_guard_world",
        "evaluate_in_state_world",
        "evaluate_by_unique_id",
        "call_guard_binding_from_new_binding",
        "script_in_guard_world",
        "create_guard_world",
    ],
)
def test_agent_cannot_reach_statelock_worlds(server: dict[str, Any], attempt: str) -> None:
    # Code in the guard world could report a trusted submit (whitelisting form.submit())
    # or silence the guard; code in the state world could change what policies see.
    async def steps(page: Any) -> Any:
        cdp, seen = await _statelock_contexts(page)
        guard, state = seen["__statelock_guard"], seen["__statelock_state"]
        if attempt == "evaluate_in_guard_world":
            return await cdp.send("Runtime.evaluate", {"expression": "1", "contextId": guard["id"]})
        if attempt == "evaluate_in_state_world":
            return await cdp.send("Runtime.evaluate", {"expression": "1", "contextId": state["id"]})
        if attempt == "evaluate_by_unique_id":
            return await cdp.send("Runtime.evaluate", {"expression": "1", "uniqueContextId": guard["uniqueId"]})
        if attempt == "call_guard_binding_from_new_binding":
            return await cdp.send("Runtime.addBinding", {"name": "__statelockGuardReport"})
        if attempt == "script_in_guard_world":
            return await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument", {"source": "1", "worldName": "__statelock_guard"}
            )
        frame_id = seen[""]["auxData"]["frameId"]
        return await cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "__statelock_state"})

    session_id, result = run_session(server, "finance_reconciliation_agent", FINANCE, steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "statelock_internals"
    assert actions(server, session_id)[-1]["verdict"]["rule"] == "statelock_internals"


def test_agent_isolated_world_and_main_world_still_work(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        cdp, seen = await _statelock_contexts(page)
        main = seen[""]
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": main["auxData"]["frameId"], "worldName": "mine"})
        evaluated = await cdp.send(
            "Runtime.evaluate", {"expression": "6 * 7", "contextId": world["executionContextId"]}
        )
        in_main = await cdp.send("Runtime.evaluate", {"expression": "document.title", "contextId": main["id"]})
        return evaluated["result"]["value"], bool(in_main["result"]["value"])

    _, result = run_session(server, "finance_reconciliation_agent", FINANCE, steps)
    assert result == (42, True)


@pytest.mark.parametrize("how", ["goto", "new_tab"])
def test_local_files_are_never_loaded(server: dict[str, Any], how: str) -> None:
    # The browser runs on the Statelock host: file: URLs would read its files.
    async def steps(page: Any) -> Any:
        if how == "goto":
            await page.goto("file:///etc/passwd")
        else:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Target.createTarget", {"url": "file:///etc/passwd"})
        return await page.content()

    _, result = run_session(server, "flow_agent", "/gov/form", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "navigation_url"


def test_page_state_is_read_in_statelock_world(server: dict[str, Any]) -> None:
    # Page code that patches DOM methods cannot change what the policy reads.
    patch = """() => {
      const get = Element.prototype.getAttribute;
      Element.prototype.getAttribute = function (name) {
        return name === 'data-statelock-value' ? '$5,000.00' : get.call(this, name);
      };
    }"""

    async def steps(page: Any) -> str:
        await page.evaluate(patch)
        await page.click("text=Mark as Paid")
        return "clicked"

    _, result = run_session(server, "finance_reconciliation_agent", "/demo/finance?scenario=mismatch", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "assert_field_equal"
    assert "4,900.00" in result.reason


def test_offscreen_script_click_is_scrolled_into_view_and_replayed(server: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.evaluate(
            "document.body.prepend(Object.assign(document.createElement('div'), {style: 'height: 4000px'}))"
        )
        assert await page.evaluate("window.scrollY") == 0
        await page.evaluate("document.querySelector('button').click()")
        await page.wait_for_selector("text=Reconciliation complete", timeout=5000)
        return str(await page.inner_text("#status"))

    session_id, result = run_session(server, "finance_reconciliation_agent", FINANCE, steps)
    assert result == "Reconciliation complete"
    replayed = [r for r in actions(server, session_id) if "statelock_replayed_script_click" in r["context"]["params"]]
    # An event whose element moved before it was sent (the viewport can still be settling)
    # is recorded as not sent and located again.
    sent = [r for r in replayed if "statelock_replay_not_sent" not in r["context"]["params"]]
    assert [r["context"]["params"]["type"] for r in sent] == ["mouseMoved", "mousePressed", "mouseReleased"]
    for record in sent:
        assert record["context"]["browser_state"]["target_element"]["text"] == "Mark as Paid"
        assert record["context"]["params"]["y"] < 720  # inside the viewport, after scrolling
    assert sent[-1]["post_verdict"]["decision"] == "allow"


def test_page_state_url_contains_ignores_query_and_fragment(server: dict[str, Any]) -> None:
    # A field scoped to /bank/ is not read on another page whose query or fragment says /bank/.
    specs = [{"name": "here", "selector": "title", "url_contains": "/gov/form"}]
    specs.append({"name": "bank", "selector": "title", "url_contains": "/bank/"})

    async def read(url: str) -> dict[str, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url)
                return dict(await page.evaluate(f"({load_js('page_state.js')})({json.dumps(specs)})"))
            finally:
                await browser.close()

    state = asyncio.run(read(server["base"] + "/gov/form?next=/bank/#/bank/"))
    assert set(state["field_values"]) == {"here"}


FIELDS_PAGE = """<html><body>
<span id=amount data-statelock-value="$5.00">$9,000.00</span>
<span data-statelock-key=marked data-statelock-value="$7.00">$1.00</span>
<progress id=progress value=0.5 max=1></progress><meter id=empty value=0></meter>
<ol><li id=item>First item</li></ol>
<span class=price style="opacity:0">$1.00</span>
<span class=price style="position:absolute;left:-9999px">$2.00</span>
<div style="opacity:0"><span class=price>$3.00</span></div>
<span class=price style="display:inline-block;width:0;height:0;overflow:hidden">$4.00</span>
<div style="height:3000px"></div><span class=price>$5.00</span>
</body></html>"""


def _page_state(html: str, specs: list[dict[str, Any]]) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return dict(await page.evaluate(f"({load_js('page_state.js')})({json.dumps(specs)})"))
            finally:
                await browser.close()

    return asyncio.run(read())


def test_selector_fields_read_what_the_page_shows() -> None:
    specs = [
        {"name": "amount", "selector": "#amount"},
        {"name": "progress", "selector": "#progress"},
        {"name": "empty", "selector": "#empty"},
        {"name": "item", "selector": "#item"},
        {"name": "bad", "selector": "#["},  # cannot be read: left out, so its rules block
        {"name": "price", "selector": ".price", "visible": True},
        {"name": "shown", "selector": ".price", "visible": True, "read": "count"},
    ]
    state = _page_state(FIELDS_PAGE, specs)
    values = state["field_values"]
    # Markup cannot replace what a policy's selector field reads (only data-statelock-key fields).
    assert values["amount"] == "$9,000.00"
    assert state["extracted_fields"] == {"marked": "$7.00"}
    # Numeric values (<progress>, <meter>, <li value>) neither break the capture nor read 0 as missing.
    assert values["progress"] == "0.5" and values["empty"] == "0"
    assert values["item"] == "First item"  # an <li> without value= reads its text, not 0
    assert "bad" not in values
    # Transparent, off-page and zero-sized elements are not shown; below the fold is.
    assert values["price"] == "$5.00" and values["shown"] == "1"
