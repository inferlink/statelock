import asyncio

from fakes import FakeConnection
from helpers import run

from statelock.core.enums import InitiatedBy, SystemRule
from statelock.proxy.attribution import AGENT_SOURCE_TAG, AGENT_SOURCE_URL, frames_tainted, stack_frames, tag_agent_code
from statelock.proxy.commands import refused_command
from statelock.proxy.guard import (
    GUARDED_EVENT_TYPES,
    PageGuard,
    TargetGuardState,
    guard_script,
    violation_method,
    violation_reason,
)
from statelock.proxy.js import load_js
from statelock.proxy.pages import PageSessions
from statelock.proxy.worlds import GUARD_BINDING


def _guard(patterns=None, violations=None, records=None, connection=None):
    connection = connection or FakeConnection()

    async def on_violation(target_id, event):
        if violations is not None:
            violations.append((target_id, event))

    async def on_record(record):
        if records is not None:
            records.append(record)

    return PageGuard(PageSessions(connection), patterns, on_violation, on_record), connection


def _state() -> TargetGuardState:
    return TargetGuardState(target_id="T1", main_frame_url="https://portal.test/gov/form")


# Tagging and refused commands -----------------------------------------------------------


def test_tag_agent_code() -> None:
    evaluate = {"method": "Runtime.evaluate", "params": {"expression": "1 + 1"}}
    call = {"method": "Runtime.callFunctionOn", "params": {"functionDeclaration": "() => 1"}}
    compile_script = {"method": "Runtime.compileScript", "params": {"expression": "x()"}}
    assert tag_agent_code(evaluate) and evaluate["params"]["expression"].endswith(AGENT_SOURCE_TAG)
    assert tag_agent_code(call) and call["params"]["functionDeclaration"].endswith(AGENT_SOURCE_TAG)
    assert tag_agent_code(compile_script) and compile_script["params"]["sourceURL"] == AGENT_SOURCE_URL
    assert not tag_agent_code({"method": "DOM.getDocument", "params": {}})


def test_stack_frames_follow_async_parents() -> None:
    stack = {"callFrames": [{"url": "a.js"}], "parent": {"callFrames": [{"url": AGENT_SOURCE_URL}]}}
    frames = stack_frames(stack)
    assert [frame["url"] for frame in frames] == ["a.js", AGENT_SOURCE_URL]
    assert frames_tainted(frames, set())
    assert frames_tainted([{"scriptId": "7"}], {"7"})
    assert not frames_tainted([{"url": "a.js", "scriptId": "1"}], {"7"})


def test_refused_commands() -> None:
    assert refused_command({"method": "Target.sendMessageToTarget"})[0] == SystemRule.WRAPPED_COMMAND.value
    assert refused_command({"method": "Debugger.setScriptSource"})[0] == SystemRule.SCRIPT_MODIFICATION.value
    assert (
        refused_command({"method": "Page.navigate", "params": {"url": " JavaScript:alert(1)"}})[0] == "javascript_url"
    )
    assert refused_command({"method": "Page.navigate", "params": {"url": "https://x.test"}}) is None


def _rule(method, url, **params):
    refused = refused_command({"method": method, "params": {"url": url, **params}})
    return refused[0] if refused else None


def test_only_web_urls_are_loaded() -> None:
    # The browser runs on the Statelock host: no local files, browser pages or agent-written documents.
    for url in (
        "file:///etc/passwd",
        " FILE:///home",
        "fi\tle:///etc/passwd",
        "data:text/html,<script>1</script>",
        "blob:https://x.test/0f0e",
        "chrome://version",
        "view-source:https://x.test",
        "about:srcdoc",
        "",
    ):
        assert _rule("Page.navigate", url) == SystemRule.NAVIGATION_URL.value, url
        assert _rule("Target.createTarget", url) == SystemRule.NAVIGATION_URL.value, url
    # An iframe navigation (frameId) is checked the same way.
    assert _rule("Page.navigate", "file:///etc/passwd", frameId="F2") == SystemRule.NAVIGATION_URL.value
    assert _rule("Target.createTarget", "javascript:1") == SystemRule.JAVASCRIPT_URL.value
    for method in ("Page.navigate", "Target.createTarget"):
        assert _rule(method, "about:blank") is None
        assert _rule(method, "http://x.test/a?b#c") is None
        assert _rule(method, "HTTPS://x.test") is None
    # Playwright reads some response bodies with Network.loadNetworkResource: web URLs only.
    assert _rule("Network.loadNetworkResource", "https://x.test/a.js", frameId="F1") is None
    assert _rule("Network.loadNetworkResource", "file:///etc/passwd") == SystemRule.NAVIGATION_URL.value
    assert _rule("Network.loadNetworkResource", "about:blank") == SystemRule.NAVIGATION_URL.value


def test_commands_outside_governance_are_refused() -> None:
    for method in ("Target.exposeDevToolsProtocol", "Network.replayXHR", "Page.setDocumentContent"):
        refused = refused_command({"method": method, "params": {}})
        assert refused is not None and refused[0] == SystemRule.UNGOVERNED_COMMAND.value, method


def test_agent_network_interception_is_refused() -> None:
    for method in (
        "Fetch.enable",
        "Fetch.fulfillRequest",
        "Fetch.continueRequest",
        "Network.setRequestInterception",
        "Network.setBlockedURLs",
    ):
        refused = refused_command({"method": method, "params": {}})
        assert refused is not None, method
        assert refused[0] == SystemRule.AGENT_NETWORK_INTERCEPTION.value
    assert refused_command({"method": "Network.enable"}) is None


# Guard script and install -----------------------------------------------------------------


def test_guard_does_not_scroll_while_reporting() -> None:
    # Reporting a synthetic click must not move the page; Statelock scrolls only to replay.
    assert "scrollIntoView" not in load_js("guard.js")


def test_guard_script_placeholders_filled() -> None:
    script = guard_script(["/demo/finance"])
    assert 'const patterns = [{"substring": "/demo/finance"}];' in script
    absolute = guard_script(["https://BANK.example:443/demo/finance/"])
    assert 'const patterns = [{"origin": "https://bank.example", "path": "/demo/finance"}];' in absolute
    assert GUARD_BINDING in script
    assert "__STATELOCK_" not in script
    for event_type in GUARDED_EVENT_TYPES:
        assert event_type in script
    assert "const patterns = null;" in guard_script(None)


def test_install_configures_target_session() -> None:
    guard, connection = _guard(["/gov/"])
    run(guard.install("T1"))
    methods = connection.methods()
    assert methods[0] == "Target.attachToTarget"
    for method in ("Network.enable", "Debugger.enable", "Runtime.addBinding", "Fetch.enable"):
        assert method in methods
    fetch = next(params for method, params, _ in connection.sent if method == "Fetch.enable")
    assert {pattern["resourceType"] for pattern in fetch["patterns"]} == {"Document", "XHR", "Fetch", "Ping"}


def test_install_retries_without_run_immediately() -> None:
    calls = []

    def add_script(params):
        calls.append(dict(params))
        if "runImmediately" in params:
            raise_error()
        return {}

    def raise_error():
        from statelock.proxy.connection import CdpError

        raise CdpError("unknown param")

    connection = FakeConnection(results={"Page.addScriptToEvaluateOnNewDocument": add_script})
    guard, _ = _guard(connection=connection)
    run(guard.install("T1"))
    assert "runImmediately" in calls[0] and "runImmediately" not in calls[1]


# Attribution --------------------------------------------------------------------------------


def test_request_from_tagged_code_is_agent_code() -> None:
    guard, _ = _guard()
    state = _state()
    guard._handle_request_will_be_sent(
        state,
        {"requestId": "R1", "initiator": {"type": "script", "stack": {"callFrames": [{"url": AGENT_SOURCE_URL}]}}},
    )
    assert state.attributions["R1"]["initiated_by"] == InitiatedBy.AGENT_CODE.value


def test_script_created_by_agent_code_taints_requests() -> None:
    guard, _ = _guard()
    state = _state()
    guard._handle_script_parsed(
        state, {"scriptId": "42", "url": "", "stackTrace": {"callFrames": [{"url": AGENT_SOURCE_URL}]}}
    )
    guard._handle_request_will_be_sent(
        state, {"requestId": "R2", "initiator": {"type": "script", "stack": {"callFrames": [{"scriptId": "42"}]}}}
    )
    assert state.attributions["R2"]["initiated_by"] == InitiatedBy.AGENT_CODE.value


def test_site_request_is_site_and_redirect_keeps_attribution() -> None:
    guard, _ = _guard()
    state = _state()
    site = {"type": "script", "stack": {"callFrames": [{"url": "https://portal.test/app.js"}]}}
    guard._handle_request_will_be_sent(state, {"requestId": "R3", "initiator": site})
    assert state.attributions["R3"]["initiated_by"] == InitiatedBy.SITE.value
    state.attributions["R4"] = {"initiated_by": InitiatedBy.AGENT_CODE.value}
    guard._handle_request_will_be_sent(state, {"requestId": "R4", "initiator": {"type": "other"}})
    assert state.attributions["R4"]["initiated_by"] == InitiatedBy.AGENT_CODE.value


def test_form_submission_without_trusted_submit_is_flagged() -> None:
    guard, _ = _guard(["/gov/"])
    state = _state()
    guard._handle_requested_navigation(state, {"reason": "formSubmissionPost", "url": "https://portal.test/gov/result"})
    assert state.untrusted_form_urls == {"https://portal.test/gov/result"}

    trusted = _state()
    guard._handle_binding(trusted, '{"kind": "trusted_submit"}')
    guard._handle_requested_navigation(
        trusted, {"reason": "formSubmissionPost", "url": "https://portal.test/gov/result"}
    )
    assert trusted.untrusted_form_urls == set()


def test_xhr_to_the_submitted_url_keeps_the_form_mark() -> None:
    guard, _ = _guard(["/gov/"])
    state = _state()
    url = "https://portal.test/gov/result"
    guard._handle_requested_navigation(state, {"reason": "formSubmissionPost", "url": url})
    assert guard._classify(state, url, "XHR", {"initiated_by": "site"})[1] is None
    assert url in state.untrusted_form_urls
    assert guard._classify(state, url, "Document", {"initiated_by": "site"})[1] == "untrusted_form_submission"
    assert state.untrusted_form_urls == set()


def test_agent_navigation_consumed_once() -> None:
    guard, _ = _guard()
    state = _state()
    guard._states["G1"] = state
    guard._note_agent_navigation("T1", "https://portal.test/gov/step3")
    assert guard._consume_agent_navigation(state, "https://portal.test/gov/step3")
    assert not guard._consume_agent_navigation(state, "https://portal.test/gov/step3")


def test_synthetic_binding_reports_violation() -> None:
    violations = []

    async def scenario():
        guard, _ = _guard(violations=violations)
        guard._handle_binding(_state(), '{"kind": "synthetic_event", "event_type": "click"}')
        await asyncio.sleep(0)
        await guard.close()

    run(scenario())
    assert violations == [("T1", {"kind": "synthetic_event", "event_type": "click"})]


def _replay_guard(results, *, active=True):
    replays, violations = [], []
    connection = FakeConnection(results=results)

    async def on_violation(target_id, event):
        violations.append((target_id, event))

    async def on_script_click(target_id, event, target):
        point = await target.locate()
        hits = await target.hits(point) if point else None
        await target.release()
        replays.append((target_id, event, point, hits))

    guard = PageGuard(
        PageSessions(connection),
        None,
        on_violation,
        on_script_click=on_script_click,
        agent_script_active=lambda _target_id: active,
    )
    return guard, connection, replays, violations


def test_agent_script_click_is_located_and_scrolled_only_for_the_replay() -> None:
    def call(params):
        if "arguments" in params:  # hits(x, y)
            return {"result": {"value": [a["value"] for a in params["arguments"]] == [5.0, 7.5]}}
        return {"result": {"value": {"x": 5, "y": 7.5}}}

    results = {"Runtime.evaluate": {"result": {"type": "object", "objectId": "O1"}}, "Runtime.callFunctionOn": call}

    async def scenario():
        guard, connection, replays, violations = _replay_guard(results)
        state = TargetGuardState(target_id="T1", session_id="G1")
        guard._handle_binding(state, '{"kind": "synthetic_event", "event_type": "click", "replay_id": 3}', 11)
        await guard.close()
        return connection, replays, violations

    connection, replays, violations = run(scenario())
    assert not violations
    click = {"kind": "synthetic_event", "event_type": "click", "replay_id": 3}
    assert replays == [("T1", click, {"x": 5.0, "y": 7.5}, True)]
    assert connection.methods() == [
        "Runtime.evaluate",
        "DOM.scrollIntoViewIfNeeded",
        "Runtime.callFunctionOn",  # the point, once the layout is still
        "Runtime.callFunctionOn",  # hits: re-checked before an event is sent
        "Runtime.releaseObject",
    ]
    locate = connection.sent[2][1]
    assert locate["awaitPromise"] is True and "requestAnimationFrame" in locate["functionDeclaration"]
    _method, params, session = connection.sent[0]
    assert params["contextId"] == 11 and "(3)" in params["expression"] and session == "G1"


def test_script_click_not_replayed_without_agent_code_or_element() -> None:
    async def scenario(active, payload):
        guard, connection, replays, violations = _replay_guard({}, active=active)
        guard._handle_binding(TargetGuardState(target_id="T1", session_id="G1"), payload, 11)
        await guard.close()
        return connection, replays, violations

    click = '{"kind": "synthetic_event", "event_type": "click", "replay_id": 3}'
    connection, replays, violations = run(scenario(False, click))  # the site's own click
    assert not replays and len(violations) == 1 and not connection.sent
    no_element = '{"kind": "synthetic_event", "event_type": "click", "replay_id": null}'
    connection, replays, violations = run(scenario(True, no_element))  # e.g. in an iframe
    assert not replays and len(violations) == 1
    # The element is gone (or was already replayed): no point, so no click.
    connection, replays, violations = run(scenario(True, click))
    assert replays == [("T1", {"kind": "synthetic_event", "event_type": "click", "replay_id": 3}, None, None)]
    assert connection.methods() == ["Runtime.evaluate"]


def test_file_input_binding_keeps_its_kind_and_explains() -> None:
    violations = []

    async def scenario():
        guard, _ = _guard(violations=violations)
        guard._handle_binding(_state(), '{"kind": "untrusted_file_input", "files": ["a.pdf"], "url": "u"}')
        guard._handle_binding(_state(), '{"kind": "made_up"}')
        await asyncio.sleep(0)
        await guard.close()

    run(scenario())
    kinds = [event["kind"] for _, event in violations]
    assert kinds == [SystemRule.UNTRUSTED_FILE_INPUT.value, SystemRule.SYNTHETIC_EVENT.value]
    assert violation_method(SystemRule.UNTRUSTED_FILE_INPUT.value) == "Statelock.untrustedFileInput"
    assert "DOM.setFileInputFiles" in violation_reason(violations[0][1])
    assert {"drop", "paste"} <= set(GUARDED_EVENT_TYPES)


def test_events_routed_by_session() -> None:
    violations = []

    async def scenario():
        guard, connection = _guard(violations=violations)
        await guard.install("T1")
        connection.emit(
            {
                "method": "Runtime.bindingCalled",
                "sessionId": "G1",
                "params": {"name": GUARD_BINDING, "payload": '{"kind":"synthetic_event"}'},
            }
        )
        connection.emit({"method": "Runtime.bindingCalled", "sessionId": "OTHER", "params": {"name": GUARD_BINDING}})
        await guard.close()

    run(scenario())
    assert len(violations) == 1


# Decisions ------------------------------------------------------------------------------------


def _decide(attribution, url="https://portal.test/gov/api/delete", resource_type="XHR", patterns=("/gov/",)):
    violations, records = [], []
    guard, connection = _guard(list(patterns), violations, records)
    state = _state()
    if attribution is not None:
        state.attributions["N1"] = attribution

    async def scenario():
        guard._states["G1"] = state
        await guard._decide(
            "G1",
            state,
            {
                "requestId": "F1",
                "networkId": "N1",
                "resourceType": resource_type,
                "request": {"url": url, "method": "POST"},
            },
        )
        await guard.close()

    run(scenario())
    return connection.methods(), violations, records


def test_decide_blocks_agent_code_request_on_governed_page() -> None:
    methods, violations, records = _decide({"initiated_by": "agent_code"})
    assert methods == ["Fetch.failRequest"]
    assert violations[0][1]["kind"] == SystemRule.AGENT_CODE_REQUEST.value
    assert records[0]["decision"] == "block"


def test_decide_allows_site_request_and_agent_code_off_governed_pages() -> None:
    methods, violations, records = _decide({"initiated_by": "site"})
    assert methods == ["Fetch.continueRequest"] and not violations
    methods, violations, records = _decide(
        {"initiated_by": "agent_code"}, url="https://open.test/api", patterns=("/nomatch/",)
    )
    assert methods == ["Fetch.continueRequest"]
    assert records[0]["initiated_by"] == "agent_code" and records[0]["governed"] is False


def test_decide_unknown_attribution_is_allowed_and_logged() -> None:
    guard_timeout_methods, _, records = _decide(None)
    assert guard_timeout_methods == ["Fetch.continueRequest"]
    assert records[0]["initiated_by"] == InitiatedBy.UNKNOWN.value


def test_install_once_keeps_what_the_guard_learned() -> None:
    # Re-attaching to a tab must not forget which scripts agent code created.
    async def scenario():
        guard, connection = _guard(["/gov/"])
        await guard.install("T1")
        state = guard._states["G1"]
        state.tainted_script_ids.add("42")
        sent = len(connection.sent)
        await guard.install("T1")
        await guard.close()
        return guard._states["G1"], len(connection.sent) - sent

    state, resent = run(scenario())
    assert state.tainted_script_ids == {"42"} and state.installed
    assert resent == 0


def _history_guard(history):
    connection = FakeConnection(results={"Page.getNavigationHistory": history})
    guard, _ = _guard(["/gov/"], connection=connection)
    state = _state()
    state.session_id = "G1"
    guard._states["G1"] = state
    return guard, state


HISTORY = {
    "currentIndex": 1,
    "entries": [
        {"id": 7, "url": "https://portal.test/gov/form"},
        {"id": 8, "url": "https://portal.test/gov/result#done"},
    ],
}


def test_reload_and_history_navigation_label_only_their_own_page_load() -> None:
    async def scenario():
        guard, state = _history_guard(HISTORY)
        guard.note_command({"method": "Page.reload", "params": {}}, "T1")
        guard.note_command({"method": "Page.navigateToHistoryEntry", "params": {"entryId": 7}}, "T1")
        await guard._navigation_lookups(state)
        urls = [navigation.url for navigation in state.agent_navigations]
        # Another page load (a site redirect elsewhere) is not the agent's navigation.
        other = guard._consume_agent_navigation(state, "https://portal.test/gov/other")
        reload = guard._consume_agent_navigation(state, "https://portal.test/gov/result")
        back = guard._consume_agent_navigation(state, "https://portal.test/gov/form")
        await guard.close()
        return urls, other, reload, back

    urls, other, reload, back = run(scenario())
    assert urls == ["https://portal.test/gov/result", "https://portal.test/gov/form"]
    assert (other, reload, back) == (False, True, True)


def test_navigation_without_a_known_url_labels_a_page_load_briefly() -> None:
    async def scenario():
        guard, state = _history_guard({"currentIndex": 0, "entries": []})
        guard.timings.unknown_navigation_window = 0.05
        guard.note_command({"method": "Page.reload", "params": {}}, "T1")
        await guard._navigation_lookups(state)
        await asyncio.sleep(0.1)
        late = guard._consume_agent_navigation(state, "https://portal.test/gov/other")
        guard.note_command({"method": "Page.reload", "params": {}}, "T1")
        await guard._navigation_lookups(state)
        soon = guard._consume_agent_navigation(state, "https://portal.test/gov/other")
        await guard.close()
        return late, soon

    assert run(scenario()) == (False, True)


def test_out_of_process_frame_of_a_governed_page_is_governed() -> None:
    guard, _ = _guard(["/gov/"])
    page = TargetGuardState(target_id="T1", session_id="G1", main_frame_url="https://portal.test/gov/form")
    frame = TargetGuardState(
        target_id="F2", session_id="G2", main_frame_url="https://ads.test/widget", parent_frame_id="T1"
    )
    guard._states.update({"G1": page, "G2": frame})
    agent = {"initiated_by": "agent_code"}
    assert guard._classify(frame, "https://ads.test/api", "XHR", agent)[1] == SystemRule.AGENT_CODE_REQUEST.value
    page.main_frame_url = "https://open.test/"
    assert guard._classify(frame, "https://ads.test/api", "XHR", agent)[1] is None
    # A frame whose page Statelock does not know is governed (fail closed).
    frame.parent_frame_id = "UNKNOWN"
    assert guard._classify(frame, "https://ads.test/api", "XHR", agent)[1] == SystemRule.AGENT_CODE_REQUEST.value
