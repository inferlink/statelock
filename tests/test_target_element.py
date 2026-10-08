"""Resolving the element an action targets (Inspector), with a fake CDP connection."""

from __future__ import annotations

from typing import Any

from fakes import FakeConnection
from helpers import key_action, mouse_action, run

from statelock.core.actions import ActionKind, CdpAction
from statelock.core.state import TargetElement
from statelock.policy.text import normalize_text
from statelock.proxy.connection import CdpError
from statelock.proxy.inspector import Inspector
from statelock.proxy.pages import PageSessions

BUTTON = {"source": "pointer", "tag_name": "BUTTON", "text": "Delete"}


def _inspector(**results: Any) -> tuple[Inspector, FakeConnection]:
    contexts = iter(range(10, 99))
    connection = FakeConnection(
        results={
            "Target.getTargets": {"targetInfos": [{"targetId": "T1", "type": "page", "url": "u"}]},
            "Page.createIsolatedWorld": lambda _params: {"executionContextId": next(contexts)},
            "Runtime.evaluate": {"result": {"value": {"url": "https://x.test/", "scroll": {"x": 0, "y": 100}}}},
            "DOM.getNodeForLocation": {"backendNodeId": 5, "frameId": "T1"},
            "DOM.describeNode": {"node": {"nodeType": 1, "nodeName": "BUTTON"}},
            "DOM.resolveNode": {"object": {"objectId": "O5"}},
            "Runtime.callFunctionOn": {"result": {"value": BUTTON}},
            **results,
        }
    )
    return Inspector(PageSessions(connection), timeout=1.0), connection


def _hits(connection: FakeConnection) -> list[dict[str, Any]]:
    return [params for method, params, _ in connection.sent if method == "DOM.getNodeForLocation"]


def test_pointer_target_is_hit_tested_in_document_coordinates() -> None:
    inspector, connection = _inspector()
    state = run(inspector.capture(mouse_action(), target_id="T1"))
    assert state.target_element == TargetElement.model_validate(BUTTON)
    assert _hits(connection) == [{"x": 10, "y": 120}]  # the viewport point plus the page's scroll
    resolve = next(params for method, params, _ in connection.sent if method == "DOM.resolveNode")
    assert resolve["backendNodeId"] == 5 and resolve["executionContextId"] == 10  # Statelock's world
    assert "Runtime.releaseObjectGroup" in connection.methods()  # the objects it held are released


def test_frame_statelock_cannot_enter_makes_the_target_unresolved() -> None:
    owner = {"node": {"nodeType": 1, "nodeName": "IFRAME", "frameId": "F2"}}
    described = {"result": {"value": {**BUTTON, "tag_name": "IFRAME", "text": "", "unresolved": True}}}
    inspector, _ = _inspector(**{"DOM.describeNode": owner, "Runtime.callFunctionOn": described})
    target = run(inspector.capture(mouse_action(), target_id="T1")).target_element
    assert target is not None and target.unresolved
    # Unknown label: counts as an interactive element with no readable label (click-text rules block).
    assert target.is_interactive and normalize_text(target.label_text) == ""


def test_pointer_outside_the_page_has_no_target_and_errors_are_unresolved() -> None:
    def no_node(_params: dict[str, Any]) -> dict[str, Any]:
        raise CdpError("No node found at given location")

    inspector, _ = _inspector(**{"DOM.getNodeForLocation": no_node})
    state = run(inspector.capture(mouse_action(), target_id="T1"))
    assert state.capture_error is None and state.target_element is None

    def broken(_params: dict[str, Any]) -> dict[str, Any]:
        raise CdpError("Execution context was destroyed.")

    inspector, _ = _inspector(**{"DOM.resolveNode": broken})
    target = run(inspector.capture(mouse_action(), target_id="T1")).target_element
    assert target is not None and target.unresolved and target.source == "pointer"


def test_focus_target_is_the_focused_element() -> None:
    def call(params: dict[str, Any]) -> dict[str, Any]:
        if params.get("returnByValue"):
            return {"result": {"value": {**BUTTON, "source": "focus"}}}
        return {"result": {"type": "object", "objectId": "focused"}}  # focused_element.js

    inspector, connection = _inspector(**{"Runtime.evaluate": _evaluate_document, "Runtime.callFunctionOn": call})
    target = run(inspector.capture(key_action(), target_id="T1")).target_element
    assert target is not None and target.source == "focus" and target.text == "Delete"
    assert not _hits(connection)


def _evaluate_document(params: dict[str, Any]) -> dict[str, Any]:
    if params["expression"] == "document":
        return {"result": {"type": "object", "objectId": "doc"}}
    return {"result": {"value": {"url": "https://x.test/"}}}


def _touch(kind: str, points: list[dict[str, Any]]) -> CdpAction:
    params = {"type": kind, "touchPoints": points}
    return CdpAction(message_id=1, method="Input.dispatchTouchEvent", kind=ActionKind.TOUCH, params=params)


def test_touch_end_uses_its_touch_start_point_once() -> None:
    inspector, connection = _inspector()
    run(inspector.capture(_touch("touchStart", [{"x": 30, "y": 40}]), target_id="T1"))
    end = _touch("touchEnd", [])
    run(inspector.capture(end, target_id="T1"))
    run(inspector.capture(end, target_id="T1"))  # the same touchEnd, captured again after the action
    run(inspector.capture(_touch("touchEnd", []), target_id="T1"))  # a stray touchEnd: no point
    assert _hits(connection) == [{"x": 30, "y": 140}] * 3


def test_touch_start_point_is_dropped_when_the_tab_detaches() -> None:
    inspector, connection = _inspector()
    run(inspector.capture(_touch("touchStart", [{"x": 30, "y": 40}]), target_id="T1"))
    connection.emit({"method": "Target.detachedFromTarget", "params": {"sessionId": "G1"}})
    run(inspector.capture(_touch("touchEnd", []), target_id="T1"))
    assert _hits(connection) == [{"x": 30, "y": 140}]
