# SPDX-License-Identifier: Apache-2.0
"""Browser state capture at interception time."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from statelock.core.actions import FOCUS_METHODS, TOUCH_METHOD, CdpAction
from statelock.core.enums import TargetSelection
from statelock.core.jsonutil import as_dict, as_str
from statelock.core.state import BrowserState, TargetElement
from statelock.policy.fields import ScopedField, resolve_fields, selector_specs
from statelock.proxy.connection import CdpError
from statelock.proxy.js import MAX_FRAME_DEPTH, load_js
from statelock.proxy.pages import PageSessions
from statelock.proxy.worlds import STATE_WORLD, IsolatedWorld

logger = logging.getLogger(__name__)

SCREENSHOT_PARAMS = {"format": "jpeg", "quality": 60, "fromSurface": True}
DOM_SNAPSHOT_DEPTH = 2
# Remote objects Statelock holds while it resolves an action's target; released after.
TARGET_OBJECT_GROUP = "statelock-target"
FRAME_OWNER_NAMES = frozenset({"IFRAME", "FRAME", "OBJECT", "EMBED", "FENCEDFRAME", "PORTAL"})
# Chromium's answer when no node is at a point (outside the page).
NO_NODE_AT_POINT = "No node found"


class Inspector:
    """Capture state from a target over Statelock's own reused CDP session.

    page_state.js runs in Statelock's isolated world, so page scripts cannot change
    what it reads (for example by patching innerText). The action's target element is
    found over CDP: a hit test for pointer actions (it enters shadow roots, open or
    closed, and in-process frames), and the focused element followed through shadow
    roots and frames for keys. A frame Statelock cannot enter (out of process) makes
    the target unresolved (see TargetElement.unresolved).
    """

    def __init__(self, pages: PageSessions, timeout: float = 2.5, fields: list[ScopedField] | None = None) -> None:
        self.pages = pages
        self.timeout = timeout
        self.fields = list(fields or [])
        self._selector_specs = selector_specs(self.fields)
        self._world = IsolatedWorld(pages, STATE_WORLD)
        # touchEnd carries no touch points: it is checked at the point its touchStart had.
        # A start point is used by one touchEnd (or touchCancel) only. Keyed by Statelock's
        # session on the tab, so a detached (closed or replaced) tab's points are dropped.
        self._touch_starts: dict[str, dict[str, float] | None] = {}
        # The touchEnd that used a start point, and the point (an action can be captured
        # again: after a review, after the action): session -> (action, point).
        self._touch_ends: dict[str, tuple[CdpAction, dict[str, float] | None]] = {}
        pages.on_detached(self._forget_session)

    def _forget_session(self, session_id: str) -> None:
        self._touch_starts.pop(session_id, None)
        self._touch_ends.pop(session_id, None)

    async def resolve_frame_target(self, frame_id: str | None) -> str | None:
        """The target whose main frame is frame_id (a tab), or None (e.g. an in-process iframe)."""
        if frame_id is None:
            return None
        targets = {str(info.get("targetId")) for info in await self.pages.list_targets()}
        return frame_id if frame_id in targets else None

    def _extracted_fields(self, page_state: dict[str, Any]) -> dict[str, Any]:
        return resolve_fields(
            self.fields,
            page_state.get("url"),
            page_state.get("extracted_fields") or {},
            page_state.get("field_values") or {},
        )

    async def capture_fields(self, target_id: str) -> BrowserState | None:
        """Light capture (URL, title, fields) for remembering values. None on failure."""
        try:
            page_state = await asyncio.wait_for(self._page_state(target_id), timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 - best effort; actions still capture and fail closed
            logger.debug("Field capture failed for %s: %s", target_id, error)
            return None
        return BrowserState(
            url=page_state.get("url"),
            title=page_state.get("title"),
            target_id=target_id,
            extracted_fields=self._extracted_fields(page_state),
        )

    async def capture(self, action: CdpAction | None = None, target_id: str | None = None) -> BrowserState:
        """Capture from target_id (the tab the action is dispatched to).

        When target_id is None, the first non-blank page is used. The page state is
        required (a failure blocks the action); the accessibility tree, layout,
        DOM snapshot and screenshot are fetched in parallel, each with its own
        timeout, and a slow one is left out instead of failing the capture.
        """
        selection = TargetSelection.ACTION_SESSION if target_id is not None else TargetSelection.HEURISTIC
        try:
            return await self._capture(action, target_id, selection)
        except Exception as error:  # noqa: BLE001 - any failure becomes a capture_error, which blocks
            logger.warning("CDP state capture failed: %s", error)
            return BrowserState(
                target_id=target_id,
                target_selection=selection,
                capture_error=str(error) or type(error).__name__,
            )

    async def _capture(
        self,
        action: CdpAction | None,
        target_id: str | None,
        selection: TargetSelection,
    ) -> BrowserState:
        required = await asyncio.wait_for(self._required(action, target_id), timeout=self.timeout)
        if isinstance(required, str):
            return BrowserState(target_id=target_id, target_selection=selection, capture_error=required)
        target, session_id, page_state = required
        accessibility_tree, viewport, dom_snapshot, screenshot = await asyncio.gather(
            self._optional(session_id, "Accessibility.getFullAXTree"),
            self._optional(session_id, "Page.getLayoutMetrics"),
            self._optional(session_id, "DOM.getDocument", {"depth": DOM_SNAPSHOT_DEPTH, "pierce": False}),
            self._optional(session_id, "Page.captureScreenshot", SCREENSHOT_PARAMS),
        )

        element = page_state.get("target_element")
        return BrowserState(
            url=page_state.get("url") or target.get("url"),
            title=page_state.get("title") or target.get("title"),
            target_id=target.get("targetId"),
            target_type=target.get("type"),
            target_selection=selection,
            target_element=TargetElement.model_validate(element) if isinstance(element, dict) else None,
            page_text=page_state.get("page_text"),
            page_text_truncated=bool(page_state.get("page_text_truncated")),
            page_text_unread=[str(item) for item in page_state.get("page_text_unread") or []],
            extracted_fields=self._extracted_fields(page_state),
            accessibility_tree=accessibility_tree or {},
            viewport=viewport or {},
            dom_snapshot=dom_snapshot or {},
            screenshot_base64=(screenshot or {}).get("data"),
        )

    async def _required(
        self, action: CdpAction | None, target_id: str | None
    ) -> tuple[dict[str, Any], str, dict[str, Any]] | str:
        """(target info, Statelock session, page state), or an error message."""
        infos = await self.pages.list_targets()
        target = find_target(infos, target_id) if target_id is not None else select_page_target(infos)
        if target is None:
            return f"Action target not found: {target_id}" if target_id else "No page target found"
        target_id = str(target["targetId"])
        session_id = await self.pages.session_for(target_id)
        page_state = await self._page_state(target_id)
        if action is not None:
            page_state["target_element"] = await self._target_element(target_id, session_id, action, page_state)
        return target, session_id, page_state

    async def _page_state(self, target_id: str) -> dict[str, Any]:
        """Run page_state.js. Raises CdpError when it throws or returns no object (the capture fails)."""
        expression = f"({load_js('page_state.js')})({json.dumps(self._selector_specs)})"
        result = await self._world.evaluate(target_id, {"expression": expression, "returnByValue": True})
        details = result.get("exceptionDetails")
        if details is not None:
            exception = as_dict(as_dict(details).get("exception"))
            raise CdpError(f"page state script failed: {exception.get('description') or as_dict(details).get('text')}")
        value = as_dict(result.get("result")).get("value")
        if not isinstance(value, dict):
            raise CdpError("page state script returned no object")
        return value

    # Target element ------------------------------------------------------------------------

    async def _target_element(
        self, target_id: str, session_id: str, action: CdpAction, page_state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The element under the pointer, or the focused one for key and text actions.

        None when there is none (no node at the point, nothing focused). When Statelock
        cannot read it (a CDP error, a frame it cannot enter), an unresolved target.
        """
        point = self._touch_point(session_id, action)
        if point is None and action.method not in FOCUS_METHODS:
            return None
        source = "pointer" if point is not None else "focus"
        try:
            if point is not None:
                return await self._pointer_target(target_id, session_id, point, as_dict(page_state.get("scroll")))
            return await self._focus_target(target_id, session_id)
        except CdpError as error:
            logger.debug("Target element of %s not resolved: %s", action.method, error)
            return {"source": source, "unresolved": True}
        finally:
            with contextlib.suppress(CdpError):
                await self.pages.connection.send(
                    "Runtime.releaseObjectGroup", {"objectGroup": TARGET_OBJECT_GROUP}, session_id=session_id
                )

    async def _pointer_target(
        self, target_id: str, session_id: str, point: dict[str, float], scroll: dict[str, Any]
    ) -> dict[str, Any] | None:
        # DOM.getNodeForLocation takes document coordinates; input events give viewport ones.
        scroll_x, scroll_y = scroll.get("x"), scroll.get("y")
        x = point["x"] + (scroll_x if isinstance(scroll_x, (int, float)) else 0)
        y = point["y"] + (scroll_y if isinstance(scroll_y, (int, float)) else 0)
        try:
            hit = await self._send(session_id, "DOM.getNodeForLocation", {"x": round(x), "y": round(y)})
        except CdpError as error:
            if NO_NODE_AT_POINT in str(error):
                return None
            raise
        backend_node_id = hit.get("backendNodeId")
        if not isinstance(backend_node_id, int):
            return None
        described = await self._send(session_id, "DOM.describeNode", {"backendNodeId": backend_node_id})
        node = as_dict(described.get("node"))
        # The hit test enters in-process frames; a frame owner hit is a frame it could not enter.
        unresolved = _is_frame_owner(node)
        resolved = await self._world.resolve_node(
            target_id,
            as_str(hit.get("frameId")),
            {"backendNodeId": backend_node_id, "objectGroup": TARGET_OBJECT_GROUP},
        )
        return await self._describe(session_id, _object_id(resolved.get("object")), "pointer", unresolved=unresolved)

    async def _focus_target(self, target_id: str, session_id: str) -> dict[str, Any] | None:
        frame_id: str | None = None  # the main frame
        element = await self._focused_in_document(target_id, session_id, frame_id)
        for _hop in range(MAX_FRAME_DEPTH):  # frames and shadow roots followed to the focused element
            if element is None:
                return None
            node = as_dict((await self._send(session_id, "DOM.describeNode", {"objectId": element})).get("node"))
            if _is_frame_owner(node):
                child_frame = as_str(node.get("frameId"))
                if child_frame is None or "contentDocument" not in node:
                    return await self._describe(session_id, element, "focus", unresolved=True)
                frame_id = child_frame
                element = await self._focused_in_document(target_id, session_id, frame_id)
                continue
            # Closed shadow roots are out of reach of page scripts; follow focus into them here.
            shadow_roots = [as_dict(root) for root in node.get("shadowRoots") or []]
            inner = None
            for shadow_root in shadow_roots:
                backend_node_id = shadow_root.get("backendNodeId")
                if shadow_root.get("shadowRootType") != "closed" or not isinstance(backend_node_id, int):
                    continue
                resolved = await self._world.resolve_node(
                    target_id,
                    frame_id,
                    {"backendNodeId": backend_node_id, "objectGroup": TARGET_OBJECT_GROUP},
                )
                inner = await self._focused_in(session_id, _object_id(resolved.get("object")))
                if inner is not None:
                    break
            if inner is None:
                return await self._describe(session_id, element, "focus", unresolved=False)
            element = inner
        raise CdpError("focus is nested too deeply to resolve")

    async def _focused_in_document(self, target_id: str, session_id: str, frame_id: str | None) -> str | None:
        document = await self._world.evaluate(
            target_id, {"expression": "document", "objectGroup": TARGET_OBJECT_GROUP}, frame_id=frame_id
        )
        return await self._focused_in(session_id, _object_id(document.get("result")))

    async def _focused_in(self, session_id: str, root: str) -> str | None:
        """The focused element of a document or shadow root (focused_element.js), or None."""
        result = await self._send(
            session_id,
            "Runtime.callFunctionOn",
            {
                "objectId": root,
                "functionDeclaration": load_js("focused_element.js"),
                "objectGroup": TARGET_OBJECT_GROUP,
            },
        )
        _raise_on_exception(result)
        value = as_dict(result.get("result"))
        object_id = value.get("objectId")
        return object_id if isinstance(object_id, str) and value.get("subtype") != "null" else None

    async def _describe(self, session_id: str, element: str, source: str, *, unresolved: bool) -> dict[str, Any] | None:
        result = await self._send(
            session_id,
            "Runtime.callFunctionOn",
            {
                "objectId": element,
                "functionDeclaration": load_js("describe_element.js"),
                "arguments": [{"value": source}, {"value": unresolved}],
                "returnByValue": True,
            },
        )
        _raise_on_exception(result)
        value = as_dict(result.get("result")).get("value")
        return value if isinstance(value, dict) else None

    async def _send(self, session_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self.pages.connection.send(method, params, session_id=session_id)

    def _touch_point(self, session_id: str, action: CdpAction) -> dict[str, float] | None:
        point = action_point(action)
        if action.method != TOUCH_METHOD:
            return point
        kind = action.params.get("type")
        ended = self._touch_ends.get(session_id)
        if ended is not None and ended[0] is action:
            return ended[1]  # the same touchEnd, captured again
        self._touch_ends.pop(session_id, None)
        if kind == "touchStart":
            self._touch_starts[session_id] = point
        elif kind in {"touchEnd", "touchCancel"}:
            start = self._touch_starts.pop(session_id, None)
            if point is None:
                self._touch_ends[session_id] = (action, start)
                return start
        return point

    async def _optional(
        self,
        session_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(
                self.pages.connection.send(method, params, session_id=session_id), timeout=self.timeout
            )
        except (CdpError, asyncio.TimeoutError) as error:
            logger.debug("Optional CDP capture command failed: %s: %s", method, error)
            return None


def action_point(action: CdpAction) -> dict[str, float] | None:
    """The viewport point of a pointer action: x/y, or the first touch point."""
    params = action.params
    touch_points = params.get("touchPoints")
    if isinstance(touch_points, list) and touch_points and isinstance(touch_points[0], dict):
        params = touch_points[0]
    x, y = params.get("x"), params.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return {"x": float(x), "y": float(y)}
    return None


def find_target(target_infos: list[dict[str, Any]], target_id: str) -> dict[str, Any] | None:
    for target in target_infos:
        if target.get("targetId") == target_id:
            return target
    return None


def select_page_target(target_infos: list[dict[str, Any]]) -> dict[str, Any] | None:
    pages = [target for target in target_infos if target.get("type") == "page" and target.get("targetId")]
    if not pages:
        return None
    non_blank = [target for target in pages if target.get("url") not in {"", "about:blank"}]
    return non_blank[0] if non_blank else pages[0]


def _is_frame_owner(node: dict[str, Any]) -> bool:
    return str(node.get("nodeName") or "").upper() in FRAME_OWNER_NAMES or (
        node.get("nodeType") == 1 and "frameId" in node
    )


def _object_id(remote_object: Any) -> str:
    object_id = as_dict(remote_object).get("objectId")
    if not isinstance(object_id, str):
        raise CdpError("no remote object")
    return object_id


def _raise_on_exception(result: dict[str, Any]) -> None:
    details = result.get("exceptionDetails")
    if details is not None:
        raise CdpError(f"target element script failed: {as_dict(details).get('text')}")
