"""Proxy internals: Statelock's worlds, capture failures, script tracking, the CDP connection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

import pytest
from fakes import FakeConnection
from helpers import context_for, mouse_action, run

from statelock.core.actions import ActionKind, CdpAction
from statelock.core.verdict import PolicyVerdict
from statelock.credentials import SecretScrubber, SecretStore
from statelock.events import Events
from statelock.policy.requests import RequestAccess, RequestRule
from statelock.proxy.attribution import AGENT_SOURCE_URL, tag_agent_code
from statelock.proxy.bridge import CdpBridge, _Session
from statelock.proxy.commands import refused_command
from statelock.proxy.connection import BrowserClosedError, CdpConnection, CdpError
from statelock.proxy.inspector import Inspector, action_point
from statelock.proxy.network_filter import filter_network_message
from statelock.proxy.pages import PageSessions
from statelock.proxy.reporter import ViolationReporter
from statelock.proxy.requests import GovernedRequests
from statelock.proxy.scripts import AgentScripts
from statelock.proxy.targets import TargetRegistry
from statelock.proxy.worlds import (
    GUARD_BINDING,
    GUARD_WORLD,
    REQUEST_WORLD,
    STATE_WORLD,
    IsolatedWorld,
    StatelockContexts,
)
from statelock.registry import ViolationRegistry

# Statelock's worlds ------------------------------------------------------------------------


def test_reload_code_is_tagged_and_agent_proxy_is_refused() -> None:
    command = {"method": "Page.reload", "params": {"scriptToEvaluateOnLoad": "fetch('/secret')"}}
    assert tag_agent_code(command)
    assert AGENT_SOURCE_URL in command["params"]["scriptToEvaluateOnLoad"]
    assert refused_command({"method": "Target.createBrowserContext", "params": {"proxyServer": "http://evil.test"}})
    assert refused_command({"method": "Security.setIgnoreCertificateErrors", "params": {"ignore": True}})
    assert refused_command({"method": "Target.createBrowserContext", "params": {}}) is None


def test_certificate_error_overrides_are_refused() -> None:
    for command in (
        {"method": "Security.setOverrideCertificateErrors", "params": {"override": True}},
        {"method": "Security.handleCertificateError", "params": {"eventId": 1, "action": "continue"}},
    ):
        refused = refused_command(command)
        assert refused is not None and refused[0] == "agent_network_interception"
    assert refused_command({"method": "Security.setOverrideCertificateErrors", "params": {"override": False}}) is None


def test_network_events_hide_cookies_and_auth_headers() -> None:
    event = {
        "method": "Network.requestWillBeSentExtraInfo",
        "params": {
            "headers": {"Cookie": "secret=1", "Authorization": "Bearer secret", "X-Ok": "yes"},
            "headersText": "Cookie: secret=1",
            "associatedCookies": [{"cookie": {"value": "secret"}}],
        },
    }
    cleaned = json.loads(filter_network_message(json.dumps(event)))
    assert "secret" not in json.dumps(cleaned)
    assert cleaned["params"]["headers"]["X-Ok"] == "yes"
    unchanged = json.dumps({"method": "Runtime.consoleAPICalled", "params": {"Cookie": "site text"}})
    assert filter_network_message(unchanged) == unchanged


def _created(context_id: int, name: str, session: str, unique: str = "") -> str:
    context = {"id": context_id, "name": name, "uniqueId": unique or f"u{context_id}", "auxData": {"frameId": "T1"}}
    return json.dumps(
        {"method": "Runtime.executionContextCreated", "sessionId": session, "params": {"context": context}}
    )


def _refusal(contexts: StatelockContexts, method: str, session: str = "A1", **params: Any) -> str | None:
    return contexts.refusal({"method": method, "sessionId": session, "params": params}, "T1")


def test_agent_commands_naming_statelock_worlds_or_binding_are_refused() -> None:
    contexts = StatelockContexts(PageSessions(FakeConnection()))
    assert _refusal(contexts, "Page.addScriptToEvaluateOnNewDocument", source="1", worldName=GUARD_WORLD)
    assert _refusal(contexts, "Page.createIsolatedWorld", frameId="T1", worldName=STATE_WORLD)
    assert _refusal(contexts, "Page.createIsolatedWorld", frameId="T1", worldName=REQUEST_WORLD)
    assert _refusal(contexts, "Runtime.addBinding", name=GUARD_BINDING)
    assert _refusal(contexts, "Runtime.addBinding", name="mine", executionContextName=GUARD_WORLD)
    assert _refusal(contexts, "Runtime.addBinding", name="mine") is None
    assert _refusal(contexts, "Page.createIsolatedWorld", frameId="T1", worldName="mine") is None


def test_statelock_contexts_announced_to_the_agent_are_refused() -> None:
    contexts = StatelockContexts(PageSessions(FakeConnection()))
    contexts.observe(_created(1, "", "A1"))
    contexts.observe(_created(2, "__playwright_utility_world", "A1"))
    contexts.observe(_created(3, GUARD_WORLD, "A1", unique="guard-unique"))
    for method, key in (
        ("Runtime.evaluate", "contextId"),
        ("Runtime.callFunctionOn", "executionContextId"),
        ("Runtime.compileScript", "executionContextId"),
        ("DOM.resolveNode", "executionContextId"),
    ):
        assert _refusal(contexts, method, **{key: 3}), method
        assert _refusal(contexts, method, **{key: 1}) is None
        assert _refusal(contexts, method, **{key: 2}) is None
    assert _refusal(contexts, "Runtime.evaluate", uniqueContextId="guard-unique")
    assert _refusal(contexts, "Runtime.evaluate", uniqueContextId="u1") is None
    # Ids are per target: another agent session's context 3 is not Statelock's.
    assert _refusal(contexts, "Runtime.evaluate", session="A2", contextId=3) is None
    # A navigation clears the page's contexts (ids can be reused after a process swap).
    contexts.observe(json.dumps({"method": "Runtime.executionContextsCleared", "sessionId": "A1", "params": {}}))
    assert _refusal(contexts, "Runtime.evaluate", contextId=3) is None


def test_statelock_contexts_are_known_from_statelock_connection_first() -> None:
    async def scenario() -> StatelockContexts:
        connection = FakeConnection()
        pages = PageSessions(connection)
        contexts = StatelockContexts(pages)
        await pages.session_for("T1")  # Statelock's session G1 on target T1
        connection.emit(json.loads(_created(7, STATE_WORLD, "G1")))
        return contexts

    contexts = run(scenario())
    # The agent has not been told about context 7 yet; its session is on T1 too.
    assert _refusal(contexts, "Runtime.evaluate", contextId=7)
    assert contexts.refusal({"method": "Runtime.evaluate", "params": {"contextId": 7}}, "T2") is None


def _world_connection(evaluate: Any) -> FakeConnection:
    worlds = iter(range(10, 20))
    return FakeConnection(
        results={
            "Page.createIsolatedWorld": lambda _params: {"executionContextId": next(worlds)},
            "Runtime.evaluate": evaluate,
        }
    )


def test_isolated_world_is_reused_and_recreated_only_when_gone() -> None:
    calls: list[int] = []

    def evaluate(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["contextId"])
        if len(calls) == 2:
            raise CdpError("Cannot find context with specified id", -32000)
        return {"result": {"value": len(calls)}}

    connection = _world_connection(evaluate)

    async def scenario() -> list[Any]:
        world = IsolatedWorld(PageSessions(connection), STATE_WORLD)
        return [await world.evaluate("T1", {"expression": "1"}) for _ in range(2)]

    results = run(scenario())
    assert calls == [10, 10, 11]  # reused, then recreated after the page navigated
    assert results[1] == {"result": {"value": 3}}
    assert connection.methods().count("Page.createIsolatedWorld") == 2


@pytest.mark.parametrize("error", ["Runtime.evaluate timed out", "Execution context was destroyed."])
def test_isolated_world_never_retries_code_that_may_have_run(error: str) -> None:
    # A Statelock.fetch POST must not be sent twice.
    calls: list[int] = []

    def evaluate(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["contextId"])
        if len(calls) > 1:
            raise CdpError(error)
        return {}

    async def scenario() -> None:
        world = IsolatedWorld(PageSessions(_world_connection(evaluate)), STATE_WORLD)
        await world.evaluate("T1", {"expression": "1"})
        await world.evaluate("T1", {"expression": "1"})

    with pytest.raises(CdpError):
        run(scenario())
    assert len(calls) == 2


def test_isolated_world_forgets_contexts_cleared_by_navigation() -> None:
    calls: list[int] = []

    def evaluate(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["contextId"])
        return {}

    connection = _world_connection(evaluate)

    async def scenario() -> None:
        world = IsolatedWorld(PageSessions(connection), STATE_WORLD)
        await world.evaluate("T1", {"expression": "1"})
        connection.emit({"method": "Runtime.executionContextsCleared", "sessionId": "G1", "params": {}})
        await world.evaluate("T1", {"expression": "1"})

    run(scenario())
    assert calls == [10, 11]


# State capture -----------------------------------------------------------------------------


def _inspector(evaluate: Any) -> Inspector:
    connection = _world_connection(evaluate)
    connection.results["Target.getTargets"] = {"targetInfos": [{"targetId": "T1", "type": "page", "url": "u"}]}
    return Inspector(PageSessions(connection), timeout=1.0)


@pytest.mark.parametrize(
    "answer",
    [
        {"result": {"type": "object"}, "exceptionDetails": {"text": "Uncaught", "exception": {"description": "boom"}}},
        {"result": {"type": "string", "value": "not an object"}},
        {"result": {"type": "undefined"}},
    ],
)
def test_a_failing_page_state_script_fails_the_capture(answer: dict[str, Any]) -> None:
    # An empty capture would let checks pass; the action must block instead.
    inspector = _inspector(lambda _params: answer)
    state = run(inspector.capture(mouse_action(), target_id="T1"))
    assert state.capture_error
    assert run(inspector.capture_fields("T1")) is None


def test_page_state_runs_in_statelock_world() -> None:
    inspector = _inspector(lambda _params: {"result": {"value": {"url": "https://x.test/", "title": "X"}}})
    state = run(inspector.capture(mouse_action(), target_id="T1"))
    assert state.capture_error is None and state.url == "https://x.test/"
    connection = inspector.pages.connection
    world = next(params for method, params, _ in connection.sent if method == "Page.createIsolatedWorld")
    evaluate = next(params for method, params, _ in connection.sent if method == "Runtime.evaluate")
    assert world["worldName"] == STATE_WORLD and evaluate["contextId"] == 10


def test_touch_actions_point_at_their_first_touch() -> None:
    def touch(points: list[dict[str, Any]]) -> CdpAction:
        params = {"type": "touchStart", "touchPoints": points}
        return CdpAction(message_id=1, method="Input.dispatchTouchEvent", kind=ActionKind.TOUCH, params=params)

    assert action_point(touch([{"x": 3, "y": 4}, {"x": 9, "y": 9}])) == {"x": 3.0, "y": 4.0}
    assert action_point(touch([])) is None
    assert action_point(mouse_action()) == {"x": 10.0, "y": 20.0}


# Agent scripts ---------------------------------------------------------------------------


def test_running_script_ends_when_its_tab_detaches_or_it_is_stale() -> None:
    scripts = AgentScripts(grace=0.0)
    scripts.started({"id": 5, "method": "Runtime.evaluate", "sessionId": "A1"}, "T1")
    assert scripts.active("T1")
    detached = {"method": "Target.detachedFromTarget", "params": {"sessionId": "A1", "targetId": "T1"}}
    scripts.observe(json.dumps(detached))
    assert not scripts.active("T1")

    stale = AgentScripts(grace=0.0, max_running=0.0)
    stale.started({"id": 6, "method": "Runtime.callFunctionOn", "sessionId": "A1"}, "T1")
    assert not stale.active("T1")


# Answers to the agent ------------------------------------------------------------------


class _ClientSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        raise AssertionError("only the client pump reads the agent's socket")

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


def test_terminate_answers_in_band_without_reading_the_agent_socket() -> None:
    client = _ClientSocket()
    reporter = ViolationReporter(client, ViolationRegistry(), Events())  # type: ignore[arg-type]
    action = CdpAction(message_id=9, method="Statelock.fetch", kind=ActionKind.PROTOCOL, params={}, session_id="A1")
    verdict = PolicyVerdict.block(reason="no", rule="secret_injection")
    run(reporter.terminate(context_for(action), verdict, 4403, answer=action))
    response = json.loads(client.sent[0])
    assert response["id"] == 9 and response["sessionId"] == "A1" and "secret_injection" in response["error"]["message"]
    assert client.closed is not None and client.closed[0] == 4403


def _session(**components: Any) -> _Session:
    """A session with only the given components (the rest are not used by the test)."""
    fields = {
        name: None
        for name in (
            "client_ws",
            "agent_channel",
            "governor",
            "targets",
            "reporter",
            "guard",
            "uploads",
            "commands",
            "cookies",
            "requests",
            "contexts",
            "scrubber",
            "scripts",
            "field_watcher",
            "replayer",
        )
    }
    return _Session(**{**fields, **components})


def test_local_answers_are_scrubbed_of_injected_secrets() -> None:
    client = _ClientSocket()
    scrubber = SecretScrubber()
    scrubber.add("hunter2")
    reporter = ViolationReporter(client, ViolationRegistry(), Events())  # type: ignore[arg-type]
    session = _session(client_ws=client, reporter=reporter, scrubber=scrubber)
    run(session.respond({"id": 1}, {"result": {"cookies": [{"value": "hunter2"}]}}))
    assert "hunter2" not in client.sent[0] and "[SECRET]" in client.sent[0]


# CDP connection ------------------------------------------------------------------------


def test_cdp_error_is_not_a_runtime_error() -> None:
    # Code that catches RuntimeError (closed WebSockets) must not swallow CDP failures.
    assert not issubclass(CdpError, RuntimeError)


class _EndedChannel:
    """A channel whose browser is gone but that still accepts sends (nothing will answer)."""

    def send(self, _message: str) -> None:
        return None

    async def recv(self) -> str:
        raise BrowserClosedError("closed")


def test_send_fails_at_once_after_the_reader_stopped() -> None:
    async def scenario() -> float:
        connection = CdpConnection(_EndedChannel(), command_timeout=30.0)  # type: ignore[arg-type]
        connection.open()
        for _ in range(2):
            await asyncio.sleep(0)  # the reader sees the end: nothing can answer any more
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(CdpError):
            await connection.send("Runtime.evaluate")
        return loop.time() - started

    assert run(scenario()) < 1.0


def _governed_requests(value: dict[str, Any], *, max_concurrent: int = 4) -> GovernedRequests:
    """GovernedRequests whose page fetch() returns value; the session injected "hunter2"."""
    connection = _world_connection(lambda _params: {"result": {"value": value}})
    connection.results["Target.getTargets"] = {"targetInfos": [{"targetId": "T1", "type": "page"}]}
    scrubber = SecretScrubber()
    scrubber.add("hunter2")
    return GovernedRequests(
        PageSessions(connection),
        TargetRegistry(),
        RequestAccess([RequestRule(url_pattern="^https://x\\.test/")]),
        agent_id="agent",
        secrets=SecretStore(),
        scrubber=scrubber,
        max_concurrent=max_concurrent,
    )


def _fetch(requests: GovernedRequests, url: str = "https://x.test/me") -> Any:
    return run(requests.answer({"id": 1, "method": "Statelock.fetch", "params": {"url": url}}))


def test_a_fetch_on_no_page_session_runs_in_a_page_with_a_document() -> None:
    requests = _governed_requests({"status": 200, "url": "https://x.test/me", "headers": [], "body": ""})
    connection = requests.pages.connection
    connection.results["Target.getTargets"] = {  # type: ignore[attr-defined]
        "targetInfos": [
            {"targetId": "BLANK", "type": "page", "url": "about:blank"},
            {"targetId": "W1", "type": "worker", "url": "https://x.test/w.js"},
            {"targetId": "T2", "type": "page", "url": "https://x.test/app"},
        ]
    }
    assert not _fetch(requests).verdict.blocked
    attached = [params["targetId"] for method, params, _ in connection.sent if method == "Target.attachToTarget"]  # type: ignore[attr-defined]
    assert attached == ["T2"]


def test_fetch_responses_are_scrubbed_of_any_secret_the_session_injected() -> None:
    # A secret typed earlier (not sent with this request) may be echoed anywhere in the answer.
    raw_body = b'{"password": "hunter2"}'
    value = {
        "status": 200,
        "statusText": "OK hunter2",
        "url": "https://x.test/me?echo=hunter2",
        "headers": [["x-echo", "hunter2"], ["x-hunter2", "1"]],
        "body": base64.b64encode(raw_body).decode("ascii"),
    }
    outcome = _fetch(_governed_requests(value))
    result = outcome.body["result"]
    assert "hunter2" not in json.dumps(outcome.body) and "hunter2" not in json.dumps(outcome.recorded)
    assert b"hunter2" not in base64.b64decode(result["body"])
    assert result["headers"] == [["x-echo", "[SECRET]"], ["x-[SECRET]", "1"]]
    assert result["statusText"] == "OK [SECRET]" and result["url"] == "https://x.test/me?echo=[SECRET]"
    assert outcome.recorded["final_url"] == "https://x.test/me?echo=[SECRET]"
    assert outcome.recorded["response_sha256"] == hashlib.sha256(raw_body).hexdigest()  # what the server sent


def test_fetch_failures_and_redirects_are_scrubbed_too() -> None:
    failed = _fetch(_governed_requests({"error": "fetch_failed", "message": "bad hunter2"}))
    redirected = _fetch(_governed_requests({"status": 200, "url": "https://elsewhere.test/?k=hunter2", "body": ""}))
    for outcome in (failed, redirected):
        assert "hunter2" not in json.dumps(outcome.body) + json.dumps(outcome.recorded) + outcome.verdict.reason
    assert redirected.verdict.blocked


def test_fetch_url_is_checked_and_fetched_in_the_browsers_form() -> None:
    fetched: list[str] = []

    def evaluate(params: dict[str, Any]) -> dict[str, Any]:
        fetched.append(params["expression"])
        return {"result": {"value": {"status": 200, "url": "https://x.test/a%22b", "body": ""}}}

    requests = _governed_requests({})
    requests.pages.connection.results["Runtime.evaluate"] = evaluate  # type: ignore[attr-defined]
    outcome = _fetch(requests, 'HTTPS://X.TEST:443/a"b#fragment')
    assert outcome.recorded["url"] == "https://x.test/a%22b" and not outcome.verdict.blocked
    assert '"url": "https://x.test/a%22b"' in fetched[0]
    refused = _fetch(requests, "https://x.test\\@api.bank.example/x")
    assert refused.verdict.blocked and refused.verdict.rule == "request_access" and len(fetched) == 1


def test_fetches_beyond_the_concurrency_limit_are_declined_and_recorded() -> None:
    async def scenario() -> tuple[Any, Any, list[Any], list[str]]:
        requests = _governed_requests({}, max_concurrent=2)
        blocker = asyncio.Event()

        async def held(_outcome: Any) -> None:
            await blocker.wait()

        for _ in range(2):
            requests.start({"id": 1, "method": "Statelock.fetch", "params": {"url": "https://x.test/me"}}, held)
        recorded: list[Any] = []

        class _Governor:
            async def record_command(self, payload: dict[str, Any], verdict: PolicyVerdict) -> None:
                recorded.append((payload, verdict))

        client = _ClientSocket()
        reporter = ViolationReporter(client, ViolationRegistry(), Events())  # type: ignore[arg-type]
        session = _session(
            client_ws=client, governor=_Governor(), reporter=reporter, requests=requests, scrubber=SecretScrubber()
        )
        payload = {"id": 5, "method": "Statelock.fetch", "params": {"url": "https://x.test/me", "body": "QUJD"}}
        await CdpBridge(None)._start_request(session, payload)  # type: ignore[arg-type]
        declined_while_busy = requests.busy(payload)
        blocker.set()
        await asyncio.sleep(0.05)
        free = requests.busy(payload)
        await requests.close()
        return declined_while_busy, free, recorded, client.sent

    busy, free, recorded, sent = run(scenario())
    assert busy is not None and free is None
    payload, verdict = recorded[0]
    assert verdict.blocked and verdict.rule == "request_concurrency"
    assert payload["params"] == {"url": "https://x.test/me", "running": 2}  # the body is not kept
    answer = json.loads(sent[0])
    assert answer["id"] == 5 and "2 requests are already running" in answer["error"]["message"]
