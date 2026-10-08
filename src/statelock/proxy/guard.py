# SPDX-License-Identifier: Apache-2.0
"""Page guard: enforce what started clicks, submits and requests on governed pages.

For each page and iframe target the agent uses, the guard configures
Statelock's own CDP session (see PageSessions) and enforces three things on
governed pages (URL matches the agent's policies):

1. Synthetic events. A listener in an isolated world (guard.js) cancels
   untrusted click, dblclick, auxclick, submit, drop and paste events
   (element.click(), dispatchEvent(...)) before any page handler runs. A plain
   click from agent code (frameworks that click with element.click()) is
   replayed as real mouse input at the element (scrolled into view first),
   governed like the agent's own clicks; other synthetic events are violations. It also clears files that
   code put into a file input (input.files = ..., Playwright set_input_files
   over a remote connection).
2. Requests started by agent code. Page loads, XHR, fetch and beacons are
   paused (Fetch.enable). A request whose initiator stack contains agent
   code (see attribution.py) is failed; others are released.
3. form.submit() from code. A form-submission navigation without a trusted
   submit event just before it was started by code, and is failed.

Every paused request is reported with its attribution for network.jsonl.
See COVERAGE.md for what is and is not analyzed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from statelock.core.enums import InitiatedBy, SystemRule
from statelock.core.jsonutil import as_dict, as_str
from statelock.core.urls import origin_and_path, url_in_scope
from statelock.proxy.attribution import AGENT_SOURCE_URL, REQUEST_SOURCE_URL, frames_tainted, stack_frames
from statelock.proxy.commands import AGENT_NAVIGATION_METHODS
from statelock.proxy.connection import CdpError
from statelock.proxy.js import load_js
from statelock.proxy.pages import PageSessions
from statelock.proxy.tasks import BackgroundTasks
from statelock.proxy.worlds import GUARD_BINDING, GUARD_WORLD

logger = logging.getLogger(__name__)

REPLAY_ELEMENT_FUNCTION = "__statelockReplayElement"
GUARDED_EVENT_TYPES = ("click", "dblclick", "auxclick", "submit", "drop", "paste")
# Violation kinds guard.js may report; anything else is recorded as a synthetic event.
GUARD_SCRIPT_VIOLATIONS = {SystemRule.SYNTHETIC_EVENT.value, SystemRule.UNTRUSTED_FILE_INPUT.value}
# Chromium rejects EventSource as a Fetch interception type.
INTERCEPTED_RESOURCE_TYPES = ("Document", "XHR", "Fetch", "Ping")
ASYNC_STACK_DEPTH = 32
GUARDED_TARGET_TYPES = {"page", "iframe"}
ATTRIBUTION_FRAMES_RECORDED = 8  # initiator stack frames kept in network.jsonl
CLOSE_DRAIN_TIMEOUT = 5.0  # seconds for in-flight violation handlers to finish recording
FORM_SUBMISSION_REASONS = {"formSubmissionGet", "formSubmissionPost"}

# Where a real click on a replayed element lands: its center, once the layout has
# stopped moving (a scroll or a viewport resize can still be settling), when the
# element itself is there (not covered, not zero-sized); else null.
REPLAY_POINT_FUNCTION = """async function () {
  const frame = () => new Promise((resolve) => { requestAnimationFrame(() => resolve()); setTimeout(resolve, 100); });
  const center = () => {
    if (!this.isConnected) return null;
    const rect = this.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
  };
  let point = center();
  for (let i = 0; point && i < 10; i++) {
    await frame();
    const next = center();
    if (next && next.x === point.x && next.y === point.y) break;
    point = next;
  }
  if (!point) return null;
  const hit = document.elementFromPoint(point.x, point.y);
  return hit && (hit === this || this.contains(hit)) ? point : null;
}"""
# Whether the element is still what a click at (x, y) hits.
REPLAY_HITS_FUNCTION = """function (x, y) {
  const hit = document.elementFromPoint(x, y);
  return !!hit && (hit === this || this.contains(hit));
}"""

ViolationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class ReplayTarget:
    """The element of a script click (kept by guard.js in the guard world), held while
    Statelock replays the click. ``locate`` scrolls it into view and returns where a
    real click would hit it; ``hits`` re-checks that point just before an event is sent."""

    def __init__(self, pages: PageSessions, session_id: str, context_id: int, replay_id: int) -> None:
        self.pages = pages
        self.session_id = session_id
        self.context_id = context_id
        self.replay_id = replay_id
        self._object_id: str | None = None
        self._fetched = False

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self.pages.connection.send(method, params, session_id=self.session_id)

    async def _element(self) -> str | None:
        if not self._fetched:
            self._fetched = True
            found = await self._send(
                "Runtime.evaluate",
                {"expression": f"globalThis.{REPLAY_ELEMENT_FUNCTION}({self.replay_id})", "contextId": self.context_id},
            )
            object_id = as_dict(found.get("result")).get("objectId")
            self._object_id = object_id if isinstance(object_id, str) else None
        return self._object_id  # None: not a plain top-frame click, or already replayed

    async def locate(self) -> dict[str, float] | None:
        object_id = await self._element()
        if object_id is None:
            return None
        await self._send("DOM.scrollIntoViewIfNeeded", {"objectId": object_id})
        located = await self._send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": REPLAY_POINT_FUNCTION,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        point = as_dict(as_dict(located.get("result")).get("value"))
        x, y = point.get("x"), point.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return {"x": float(x), "y": float(y)}
        return None

    async def hits(self, point: dict[str, float]) -> bool:
        if self._object_id is None:
            return False
        checked = await self._send(
            "Runtime.callFunctionOn",
            {
                "objectId": self._object_id,
                "functionDeclaration": REPLAY_HITS_FUNCTION,
                "arguments": [{"value": point["x"]}, {"value": point["y"]}],
                "returnByValue": True,
            },
        )
        return as_dict(checked.get("result")).get("value") is True

    async def release(self) -> None:
        if self._object_id is not None:
            with contextlib.suppress(CdpError):
                await self._send("Runtime.releaseObject", {"objectId": self._object_id})
            self._object_id = None


# (target_id, synthetic click event, its element) -> replay it as governed input.
ScriptClickHandler = Callable[[str, dict[str, Any], ReplayTarget], Awaitable[None]]
RequestRecordHandler = Callable[[dict[str, Any]], Awaitable[None]]


def guard_script(url_patterns: list[str] | None) -> str:
    """guard.js with its placeholders filled. url_patterns None guards every page."""
    scopes = None
    if url_patterns is not None:
        scopes = []
        for pattern in url_patterns:
            if pattern.startswith(("http://", "https://")):
                parsed = origin_and_path(pattern)
                scopes.append({"origin": parsed[0], "path": parsed[1].rstrip("/")} if parsed else {"origin": ""})
            else:
                scopes.append({"substring": pattern})
    return (
        load_js("guard.js")
        .replace("__STATELOCK_PATTERNS__", json.dumps(scopes))
        .replace("__STATELOCK_BINDING__", GUARD_BINDING)
        .replace("__STATELOCK_REPLAY_ELEMENT__", REPLAY_ELEMENT_FUNCTION)
        .replace("__STATELOCK_EVENTS__", json.dumps(list(GUARDED_EVENT_TYPES)))
    )


@dataclass
class GuardTimings:
    attribution_timeout: float = 1.0
    trusted_submit_window: float = 3.0
    agent_navigation_window: float = 10.0


@dataclass
class TargetGuardState:
    target_id: str
    session_id: str = ""  # Statelock's CDP session on the target
    main_frame_url: str = ""
    tainted_script_ids: set[str] = field(default_factory=set)
    # Network requestId -> attribution record.
    attributions: dict[str, dict[str, Any]] = field(default_factory=dict)
    attribution_waiters: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    trusted_submit_at: float | None = None
    untrusted_form_urls: set[str] = field(default_factory=set)
    # (url or None for reload/history, monotonic time) of agent navigation commands.
    agent_navigations: list[tuple[str | None, float]] = field(default_factory=list)


class PageGuard:
    def __init__(
        self,
        pages: PageSessions,
        url_patterns: list[str] | None,
        on_violation: ViolationHandler,
        on_request_record: RequestRecordHandler | None = None,
        *,
        timings: GuardTimings | None = None,
        on_script_click: ScriptClickHandler | None = None,
        agent_script_active: Callable[[str], bool] = lambda _target_id: False,
    ) -> None:
        self.pages = pages
        self.url_patterns = url_patterns
        self.on_violation = on_violation
        self.on_request_record = on_request_record
        self.timings = timings or GuardTimings()
        self.on_script_click = on_script_click
        self.agent_script_active = agent_script_active
        # Statelock sessionId -> per-target state
        self._states: dict[str, TargetGuardState] = {}
        self._tasks = BackgroundTasks("page guard")
        # CDP event -> handler(statelock_session_id, state, params)
        self._handlers: dict[str, Callable[[str, TargetGuardState, dict[str, Any]], None]] = {
            "Runtime.bindingCalled": self._on_binding_called,
            "Fetch.requestPaused": self._on_request_paused,
            "Debugger.scriptParsed": lambda _sid, state, params: self.handle_script_parsed(state, params),
            "Network.requestWillBeSent": lambda _sid, state, params: self.handle_request_will_be_sent(state, params),
            "Network.loadingFinished": lambda _sid, state, params: self.forget_request(state, params),
            "Network.loadingFailed": lambda _sid, state, params: self.forget_request(state, params),
            "Page.frameNavigated": lambda _sid, state, params: self.handle_frame_navigated(state, params),
            "Page.frameRequestedNavigation": lambda _sid, state, params: self.handle_requested_navigation(
                state, params
            ),
        }
        pages.connection.add_listener(self._on_event)

    async def close(self, drain_timeout: float = CLOSE_DRAIN_TIMEOUT) -> None:
        """Let in-flight violation handlers finish recording, then cancel the rest."""
        await self._tasks.close(drain_timeout)

    def setup_target(self, target_id: str, target_info: dict[str, Any]) -> Awaitable[None] | None:
        """TargetRegistry setup step: install the guard on pages and iframes."""
        if target_info.get("type") not in GUARDED_TARGET_TYPES:
            return None
        return self.install(target_id)

    def is_governed(self, *urls: str) -> bool:
        if self.url_patterns is None:
            return True
        return any(url_in_scope(pattern, url) for url in urls if url for pattern in self.url_patterns)

    async def install(self, target_id: str) -> None:
        """Configure the guard on a target. Raises CdpError on failure."""
        connection = self.pages.connection
        session_id = await self.pages.session_for(target_id)
        self._states[session_id] = TargetGuardState(target_id=target_id, session_id=session_id)
        await connection.send("Network.enable", session_id=session_id)
        await connection.send("Debugger.enable", session_id=session_id)
        await connection.send("Debugger.setAsyncCallStackDepth", {"maxDepth": ASYNC_STACK_DEPTH}, session_id=session_id)
        await connection.send("Debugger.setSkipAllPauses", {"skip": True}, session_id=session_id)
        await connection.send(
            "Runtime.addBinding",
            {"name": GUARD_BINDING, "executionContextName": GUARD_WORLD},
            session_id=session_id,
        )
        script: dict[str, Any] = {
            "source": guard_script(self.url_patterns),
            "worldName": GUARD_WORLD,
            "runImmediately": True,
        }
        try:
            await connection.send("Page.addScriptToEvaluateOnNewDocument", script, session_id=session_id)
        except CdpError:
            # Chromium without runImmediately: applies from the next document.
            script.pop("runImmediately")
            await connection.send("Page.addScriptToEvaluateOnNewDocument", script, session_id=session_id)
        await connection.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "*", "resourceType": resource_type, "requestStage": "Request"}
                    for resource_type in INTERCEPTED_RESOURCE_TYPES
                ]
            },
            session_id=session_id,
        )
        logger.info("Installed Statelock page guard target=%s", target_id)

    def note_command(self, payload: dict[str, Any], target_id: str | None) -> None:
        """Note an agent command that navigates (goto, reload, back/forward), for attribution."""
        method = payload.get("method")
        if method not in AGENT_NAVIGATION_METHODS or target_id is None:
            return
        url = as_dict(payload.get("params")).get("url") if method == "Page.navigate" else None
        self.note_agent_navigation(target_id, as_str(url))

    def note_agent_navigation(self, target_id: str, url: str | None) -> None:
        """Record an agent navigation command (goto, reload, back/forward) for attribution."""
        now = time.monotonic()
        window = self.timings.agent_navigation_window
        for state in self._states.values():
            if state.target_id == target_id:
                state.agent_navigations = [e for e in state.agent_navigations if now - e[1] <= window]
                state.agent_navigations.append((url, now))

    # Event handling -----------------------------------------------------------------

    def _on_event(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = as_dict(message.get("params"))
        if method == "Target.detachedFromTarget":
            self._states.pop(str(params.get("sessionId") or ""), None)
            return
        session_id = as_str(message.get("sessionId"))
        state = self._states.get(session_id or "")
        handler = self._handlers.get(method)
        if state is None or session_id is None or handler is None:
            return
        handler(session_id, state, params)

    def _on_binding_called(self, _session_id: str, state: TargetGuardState, params: dict[str, Any]) -> None:
        if params.get("name") == GUARD_BINDING:
            context_id = params.get("executionContextId")
            self.handle_binding(state, params.get("payload"), context_id if isinstance(context_id, int) else None)

    def _on_request_paused(self, session_id: str, state: TargetGuardState, params: dict[str, Any]) -> None:
        self._tasks.spawn(self.decide(session_id, state, params))

    def handle_frame_navigated(self, state: TargetGuardState, params: dict[str, Any]) -> None:
        frame = as_dict(params.get("frame"))
        if frame and not frame.get("parentId"):
            state.main_frame_url = str(frame.get("url") or "")

    def handle_binding(self, state: TargetGuardState, raw_payload: Any, context_id: int | None = None) -> None:
        """A report from guard.js (context_id: the guard world it came from)."""
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {"raw_payload": raw_payload}
        if not isinstance(payload, dict):
            payload = {"raw_payload": payload}
        if payload.get("kind") == "trusted_submit":
            state.trusted_submit_at = time.monotonic()
            return
        if payload.get("kind") not in GUARD_SCRIPT_VIOLATIONS:
            payload["kind"] = SystemRule.SYNTHETIC_EVENT.value
        replay_id = self._replay_id(state, payload)
        if self.on_script_click is not None and context_id is not None and replay_id is not None:
            target = ReplayTarget(self.pages, state.session_id, context_id, replay_id)
            self._tasks.spawn(self.on_script_click(state.target_id, payload, target))
            return
        self._tasks.spawn(self.on_violation(state.target_id, payload))

    def _replay_id(self, state: TargetGuardState, payload: dict[str, Any]) -> int | None:
        """The id guard.js kept a plain click's element under, when agent code made the click."""
        replay_id = payload.get("replay_id")
        replayable = (
            payload.get("kind") == SystemRule.SYNTHETIC_EVENT.value
            and payload.get("event_type") == "click"
            and isinstance(replay_id, int)
            and not isinstance(replay_id, bool)
            and self.agent_script_active(state.target_id)
        )
        return replay_id if replayable else None

    def handle_script_parsed(self, state: TargetGuardState, params: dict[str, Any]) -> None:
        script_id = params.get("scriptId")
        if not isinstance(script_id, str):
            return
        if params.get("url") == AGENT_SOURCE_URL or frames_tainted(
            stack_frames(params.get("stackTrace")), state.tainted_script_ids
        ):
            state.tainted_script_ids.add(script_id)

    def handle_request_will_be_sent(self, state: TargetGuardState, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        if not isinstance(request_id, str):
            return
        initiator = as_dict(params.get("initiator"))
        frames = stack_frames(initiator.get("stack"))
        previous = state.attributions.get(request_id)
        # A redirect keeps the attribution of the original request.
        agent = frames_tainted(frames, state.tainted_script_ids) or bool(
            previous and previous["initiated_by"] == InitiatedBy.AGENT_CODE.value
        )
        if agent:
            initiated_by = InitiatedBy.AGENT_CODE
        elif any(frame.get("url") == REQUEST_SOURCE_URL for frame in frames):
            initiated_by = InitiatedBy.STATELOCK_REQUEST
        else:
            initiated_by = InitiatedBy.SITE
        attribution = {
            "initiated_by": initiated_by.value,
            "initiator_type": initiator.get("type"),
            "initiator_frames": [
                {"url": frame.get("url"), "function": frame.get("functionName"), "line": frame.get("lineNumber")}
                for frame in frames[:ATTRIBUTION_FRAMES_RECORDED]
            ],
        }
        state.attributions[request_id] = attribution
        waiter = state.attribution_waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(attribution)

    def forget_request(self, state: TargetGuardState, params: dict[str, Any]) -> None:
        """A request finished: drop its attribution, so long-lived pages do not grow the map."""
        request_id = params.get("requestId")
        if isinstance(request_id, str):
            state.attributions.pop(request_id, None)

    def handle_requested_navigation(self, state: TargetGuardState, params: dict[str, Any]) -> None:
        if params.get("reason") not in FORM_SUBMISSION_REASONS:
            return
        url = str(params.get("url") or "")
        now = time.monotonic()
        if state.trusted_submit_at is not None and now - state.trusted_submit_at <= self.timings.trusted_submit_window:
            state.trusted_submit_at = None
            return
        if self.is_governed(state.main_frame_url, url):
            state.untrusted_form_urls.add(url)

    def consume_agent_navigation(self, state: TargetGuardState, url: str) -> bool:
        now = time.monotonic()
        for index, (expected_url, noted_at) in enumerate(state.agent_navigations):
            if now - noted_at > self.timings.agent_navigation_window:
                continue
            if expected_url is None or expected_url == url or url.startswith(expected_url):
                del state.agent_navigations[index]
                return True
        return False

    async def _attribution(self, state: TargetGuardState, network_id: str | None) -> dict[str, Any] | None:
        if network_id is None:
            return None
        known = state.attributions.get(network_id)
        if known is not None:
            return known
        waiter = state.attribution_waiters.get(network_id)
        if waiter is None:
            waiter = asyncio.get_running_loop().create_future()
            state.attribution_waiters[network_id] = waiter
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=self.timings.attribution_timeout)
        except asyncio.TimeoutError:
            state.attribution_waiters.pop(network_id, None)
            return None

    def classify(
        self,
        state: TargetGuardState,
        url: str,
        resource_type: str | None,
        attribution: dict[str, Any] | None,
    ) -> tuple[str, str | None, bool]:
        """Return (initiated_by, violation kind or None, governed) for a paused request."""
        governed = self.is_governed(state.main_frame_url, url)
        untrusted_form = resource_type == "Document" and url in state.untrusted_form_urls
        if resource_type == "Document":  # an XHR to the same URL must not clear the mark
            state.untrusted_form_urls.discard(url)

        initiated_by = attribution["initiated_by"] if attribution else InitiatedBy.UNKNOWN.value
        if untrusted_form:
            initiated_by = InitiatedBy.AGENT_CODE.value
        elif (
            resource_type == "Document"
            and initiated_by != InitiatedBy.AGENT_CODE.value
            and self.consume_agent_navigation(state, url)
        ):
            initiated_by = InitiatedBy.AGENT_NAVIGATION.value

        kind: str | None = None
        if governed and untrusted_form:
            kind = SystemRule.UNTRUSTED_FORM_SUBMISSION.value
        elif governed and initiated_by == InitiatedBy.AGENT_CODE.value:
            kind = SystemRule.AGENT_CODE_REQUEST.value
        return initiated_by, kind, governed

    async def decide(self, session_id: str, state: TargetGuardState, params: dict[str, Any]) -> None:
        """Release or fail one paused request."""
        fetch_request_id = params.get("requestId")
        request = as_dict(params.get("request"))
        url = str(request.get("url") or "")
        resource_type = as_str(params.get("resourceType"))
        try:
            attribution = await self._attribution(state, as_str(params.get("networkId")))
            initiated_by, kind, governed = self.classify(state, url, resource_type, attribution)
            record = {
                "kind": kind or "request",
                "target_id": state.target_id,
                "url": url,
                "method": request.get("method"),
                "resource_type": resource_type,
                "page_url": state.main_frame_url,
                "governed": governed,
                "initiated_by": initiated_by,
                "decision": "block" if kind else "allow",
                "initiator": attribution,
            }
            if self.on_request_record is not None:
                self._tasks.spawn(self.on_request_record(record))
            if kind is None:
                await self.pages.connection.send(
                    "Fetch.continueRequest", {"requestId": fetch_request_id}, session_id=session_id
                )
                return
            await self._fail_request(session_id, fetch_request_id)
            await self.on_violation(state.target_id, record)
        except Exception as error:  # noqa: BLE001 - fail closed: an undecided request is never released
            logger.warning("Guard decision failed for %s: %s", url, error)
            with contextlib.suppress(CdpError):
                await self._fail_request(session_id, fetch_request_id)

    async def _fail_request(self, session_id: str, fetch_request_id: Any) -> None:
        await self.pages.connection.send(
            "Fetch.failRequest",
            {"requestId": fetch_request_id, "errorReason": "BlockedByClient"},
            session_id=session_id,
        )


GUARD_VIOLATION_METHODS = {
    SystemRule.SYNTHETIC_EVENT.value: "Statelock.syntheticEvent",
    SystemRule.AGENT_CODE_REQUEST.value: "Statelock.agentCodeRequest",
    SystemRule.UNTRUSTED_FORM_SUBMISSION.value: "Statelock.untrustedFormSubmission",
    SystemRule.UNTRUSTED_FILE_INPUT.value: "Statelock.untrustedFileInput",
}


def violation_method(kind: str) -> str:
    """Artifact method name recorded for a guard violation."""
    return GUARD_VIOLATION_METHODS.get(kind, "Statelock.guardViolation")


def violation_reason(event: dict[str, Any]) -> str:
    """Agent-facing explanation of a guard violation, with what to do instead."""
    kind = event.get("kind")
    if kind == SystemRule.AGENT_CODE_REQUEST.value:
        return (
            f"Blocked {event.get('method')} {event.get('resource_type')} request to "
            f"{event.get('url')}: it was started by the agent's page code "
            "(page.evaluate or similar), not by real input or the site's own code. "
            "Use real clicks and typing, or page.goto() for navigation."
        )
    if kind == SystemRule.UNTRUSTED_FORM_SUBMISSION.value:
        return (
            f"Blocked form submission to {event.get('url')}: the form was submitted by "
            "code (form.submit()) without a real submit action. Click the submit button "
            "or press Enter in the form instead."
        )
    if kind == SystemRule.UNTRUSTED_FILE_INPUT.value:
        target = as_dict(event.get("target"))
        return (
            f"Blocked files {event.get('files')} in file input {target.get('id') or target.get('tag_name')} "
            f"at {event.get('url')}: they were set by page code, not chosen through the file chooser or "
            "DOM.setFileInputFiles. Playwright set_input_files over a remote CDP connection sets files "
            "with page code. Use StatelockConnection.set_input_files(page, selector, files) instead."
        )
    target = as_dict(event.get("target"))
    target_text = str(target.get("text") or target.get("aria_label") or "").strip()[:80]
    return (
        f"Blocked synthetic {event.get('event_type')} event "
        f"on {target.get('tag_name')} '{target_text}' at {event.get('url')}: "
        "it was not produced by real mouse or keyboard input. Statelock replays a plain "
        "element.click() from agent code as a real click only when the element is visible "
        "and not covered, in the top frame. Otherwise use locator.click() or keyboard input "
        "instead of element.click(), dispatch_event() or page code that dispatches events."
    )
