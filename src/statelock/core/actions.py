# SPDX-License-Identifier: Apache-2.0
"""Governed CDP input actions and their classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

MOUSE_METHOD = "Input.dispatchMouseEvent"
KEY_METHOD = "Input.dispatchKeyEvent"
TEXT_METHOD = "Input.insertText"
TOUCH_METHOD = "Input.dispatchTouchEvent"
DRAG_METHOD = "Input.dispatchDragEvent"
IME_METHOD = "Input.imeSetComposition"
TAP_GESTURE_METHOD = "Input.synthesizeTapGesture"
SCROLL_GESTURE_METHOD = "Input.synthesizeScrollGesture"
PINCH_GESTURE_METHOD = "Input.synthesizePinchGesture"
EMULATE_TOUCH_METHOD = "Input.emulateTouchFromMouseEvent"
FILE_UPLOAD_METHOD = "DOM.setFileInputFiles"
# Recorded by Statelock for a download the page started (not an agent command).
DOWNLOAD_METHOD = "Statelock.download"


class ActionKind(str, Enum):
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    TEXT = "text"
    TOUCH = "touch"
    DRAG = "drag"
    FILE_UPLOAD = "file_upload"
    DOWNLOAD = "download"
    # Recorded by Statelock itself, not dispatched by the agent as Input.*.
    SYNTHETIC = "synthetic"
    PROTOCOL = "protocol"


MUTATING_CDP_METHODS: dict[str, ActionKind] = {
    MOUSE_METHOD: ActionKind.MOUSE,
    KEY_METHOD: ActionKind.KEYBOARD,
    TEXT_METHOD: ActionKind.TEXT,
    TOUCH_METHOD: ActionKind.TOUCH,
    DRAG_METHOD: ActionKind.DRAG,
    IME_METHOD: ActionKind.TEXT,
    TAP_GESTURE_METHOD: ActionKind.TOUCH,
    SCROLL_GESTURE_METHOD: ActionKind.TOUCH,
    PINCH_GESTURE_METHOD: ActionKind.TOUCH,
    EMULATE_TOUCH_METHOD: ActionKind.MOUSE,
    FILE_UPLOAD_METHOD: ActionKind.FILE_UPLOAD,
}

# Actions aimed at a point on the page: rules and triggers that look at the
# element under the pointer (prohibit_click_text, trigger.click_text) apply.
POINTER_METHODS = {MOUSE_METHOD, EMULATE_TOUCH_METHOD, TOUCH_METHOD, TAP_GESTURE_METHOD, DRAG_METHOD}
# Actions aimed at the focused element.
FOCUS_METHODS = {KEY_METHOD, TEXT_METHOD, IME_METHOD}
MOUSE_LIKE_METHODS = {MOUSE_METHOD, EMULATE_TOUCH_METHOD}

# CDP key-event modifier bits (Shift=8 is left out: it does not make a shortcut).
SHORTCUT_MODIFIERS = 1 | 2 | 4  # Alt, Ctrl, Meta
KEY_DOWN_TYPES = {"keyDown", "rawKeyDown"}
RELEASE_TYPES = {"mouseReleased", "touchEnd", "keyUp"}

ENTER_KEYS = {"Enter"}
ENTER_CODES = {"Enter", "NumpadEnter"}
SPACE_KEYS = {" "}
SPACE_CODES = {"Space"}


def is_enter_key(params: dict[str, Any]) -> bool:
    return (
        params.get("key") in ENTER_KEYS
        or params.get("code") in ENTER_CODES
        or params.get("windowsVirtualKeyCode") == 13
        or (params.get("type") == "char" and params.get("text") in {"\r", "\n"})
    )


def is_pointer_method(method: str) -> bool:
    return method in POINTER_METHODS


def is_activation_key(params: dict[str, Any]) -> bool:
    """Keys that activate a focused button or link (Enter, Space)."""
    return (
        is_enter_key(params)
        or params.get("key") in SPACE_KEYS
        or params.get("code") in SPACE_CODES
        or params.get("windowsVirtualKeyCode") == 32
    )


def _is_activating_key(method: str, params: dict[str, Any]) -> bool:
    modifiers = params.get("modifiers")
    shortcut = isinstance(modifiers, int) and bool(modifiers & SHORTCUT_MODIFIERS)
    return method == KEY_METHOD and (is_activation_key(params) or shortcut)


def starts_activation(method: str, params: dict[str, Any]) -> bool:
    """The action that begins activating something: mouse or touch press, tap, drop,
    file upload, and Enter, Space or a Ctrl/Alt/Meta shortcut key press.
    Actions paused for human review are these and their releases."""
    kind = params.get("type")
    if method in MOUSE_LIKE_METHODS:
        return kind == "mousePressed"
    if method == TOUCH_METHOD:
        return kind == "touchStart"
    if method in {TAP_GESTURE_METHOD, FILE_UPLOAD_METHOD}:
        return True
    if method == DRAG_METHOD:
        return kind == "drop"
    return _is_activating_key(method, params) and (kind in KEY_DOWN_TYPES or kind == "char")


def is_commit(method: str, params: dict[str, Any]) -> bool:
    """Actions after which post-conditions run: mouse or touch release, tap, drop and Enter release."""
    kind = params.get("type")
    if method in MOUSE_LIKE_METHODS:
        return kind == "mouseReleased"
    if method == TOUCH_METHOD:
        return kind == "touchEnd"
    if method == TAP_GESTURE_METHOD:
        return True
    if method == DRAG_METHOD:
        return kind == "drop"  # the drop replaces the mouse release
    return method == KEY_METHOD and kind in {"keyUp", "char"} and is_enter_key(params)


def release_key(method: str, params: dict[str, Any], session_id: str | None) -> tuple[str, ...] | None:
    """For a press that starts an activation, what its release looks like; for a
    release, the same key. An approved press covers its matching release."""
    kind = params.get("type")
    if method in MOUSE_LIKE_METHODS and kind in {"mousePressed", "mouseReleased"}:
        return (method, str(session_id), str(params.get("button")))
    if method == TOUCH_METHOD and kind in {"touchStart", "touchEnd"}:
        return (method, str(session_id))
    if _is_activating_key(method, params) and (kind in KEY_DOWN_TYPES or kind == "keyUp"):
        return (method, str(session_id), str(params.get("code") or params.get("key")))
    return None


def ends_activation(method: str, params: dict[str, Any]) -> bool:
    """The release of an activation: mouse release, touch end, or the key up of an activating key."""
    return params.get("type") in RELEASE_TYPES and release_key(method, params, None) is not None


@dataclass(frozen=True)
class CdpAction:
    message_id: int | None
    method: str
    kind: ActionKind
    params: dict[str, Any]
    session_id: str | None = None

    @property
    def is_commit(self) -> bool:
        return is_commit(self.method, self.params)

    @property
    def starts_activation(self) -> bool:
        return starts_activation(self.method, self.params)

    @property
    def ends_activation(self) -> bool:
        return ends_activation(self.method, self.params)

    @property
    def release_key(self) -> tuple[str, ...] | None:
        return release_key(self.method, self.params, self.session_id)


def parse_cdp_message(raw_message: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def classify_cdp_action(payload: dict[str, Any]) -> CdpAction | None:
    method = payload.get("method")
    if not isinstance(method, str):
        return None
    kind = MUTATING_CDP_METHODS.get(method)
    if kind is None:
        return None
    message_id = payload.get("id")
    params = payload.get("params", {})
    session_id = payload.get("sessionId")
    return CdpAction(
        message_id=message_id if isinstance(message_id, int) else None,
        method=method,
        kind=kind,
        params=params if isinstance(params, dict) else {},
        session_id=session_id if isinstance(session_id, str) else None,
    )


def protocol_action(payload: dict[str, Any]) -> CdpAction:
    """Wrap any CDP command as a PROTOCOL action (for refused commands)."""
    message_id = payload.get("id")
    params = payload.get("params")
    session_id = payload.get("sessionId")
    return CdpAction(
        message_id=message_id if isinstance(message_id, int) else None,
        method=str(payload.get("method") or ""),
        kind=ActionKind.PROTOCOL,
        params=params if isinstance(params, dict) else {},
        session_id=session_id if isinstance(session_id, str) else None,
    )
