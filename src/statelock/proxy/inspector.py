# SPDX-License-Identifier: Apache-2.0
"""Browser state capture at interception time."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from statelock.core.actions import FOCUS_METHODS, TOUCH_METHOD, CdpAction
from statelock.core.enums import TargetSelection
from statelock.core.jsonutil import as_dict
from statelock.core.state import BrowserState, TargetElement
from statelock.policy.fields import ScopedField, resolve_fields, selector_specs
from statelock.proxy.connection import CdpError
from statelock.proxy.js import load_js
from statelock.proxy.pages import PageSessions
from statelock.proxy.worlds import STATE_WORLD, IsolatedWorld

logger = logging.getLogger(__name__)

SCREENSHOT_PARAMS = {"format": "jpeg", "quality": 60, "fromSurface": True}
DOM_SNAPSHOT_DEPTH = 2


class Inspector:
    """Capture state from a target over Statelock's own reused CDP session.

    page_state.js runs in Statelock's isolated world, so page scripts cannot change
    what it reads (for example by patching innerText or elementFromPoint).
    """

    def __init__(self, pages: PageSessions, timeout: float = 2.5, fields: list[ScopedField] | None = None) -> None:
        self.pages = pages
        self.timeout = timeout
        self.fields = list(fields or [])
        self._selector_specs = selector_specs(self.fields)
        self._world = IsolatedWorld(pages, STATE_WORLD)
        # touchEnd carries no touch points: it is checked at the point its touchStart had.
        self._touch_starts: dict[str, dict[str, float]] = {}

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
            page_state = await asyncio.wait_for(self._page_state(target_id, None), timeout=self.timeout)
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
        session_id = await self.pages.session_for(str(target["targetId"]))
        return target, session_id, await self._page_state(str(target["targetId"]), action)

    async def _page_state(self, target_id: str, action: CdpAction | None) -> dict[str, Any]:
        """Run page_state.js. Raises CdpError when it throws or returns no object (the capture fails)."""
        point = self._touch_point(target_id, action) if action is not None else None
        use_focus = point is None and action is not None and action.method in FOCUS_METHODS
        arguments = ", ".join(json.dumps(value) for value in (point, use_focus, self._selector_specs))
        expression = f"({load_js('page_state.js')})({arguments})"
        result = await self._world.evaluate(target_id, {"expression": expression, "returnByValue": True})
        details = result.get("exceptionDetails")
        if details is not None:
            exception = as_dict(as_dict(details).get("exception"))
            raise CdpError(f"page state script failed: {exception.get('description') or as_dict(details).get('text')}")
        value = as_dict(result.get("result")).get("value")
        if not isinstance(value, dict):
            raise CdpError("page state script returned no object")
        return value

    def _touch_point(self, target_id: str, action: CdpAction) -> dict[str, float] | None:
        point = action_point(action)
        if action.method != TOUCH_METHOD:
            return point
        kind = action.params.get("type")
        if kind == "touchStart" and point is not None:
            self._touch_starts[target_id] = point
        elif kind in {"touchEnd", "touchCancel"} and point is None:
            return self._touch_starts.get(target_id)
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
