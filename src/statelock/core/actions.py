# SPDX-License-Identifier: Apache-2.0
"""Governed CDP input actions and their classification."""

from __future__ import annotations

from dataclasses import dataclass, replace
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

# Enter and Space, by key name, code, Windows key code, or the text a key event types
# (text or unmodifiedText). Chromium acts on any of these, and so may page scripts.
ENTER_KEYS = {"Enter", "\r", "\n"}
ENTER_CODES = {"Enter", "NumpadEnter"}
ENTER_TEXTS = {"\r", "\n", "\r\n"}
SPACE_KEYS = {" ", "Spacebar"}
SPACE_CODES = {"Space"}
SPACE_TEXTS = {" "}
ENTER_KEY_CODE = 13
SPACE_KEY_CODE = 32
# Set by the governor on a char event that types the key a preceding rawKeyDown (or a
# keyDown without text) on the same CDP session already pressed: that key's activation
# began at the key down and commits at its key up (see KeyPresses).
FOLLOWS_KEY_DOWN_PARAM = "statelock_follows_key_down"


def _named(params: dict[str, Any], keys: set[str], codes: set[str], key_code: int) -> bool:
    return params.get("key") in keys or params.get("code") in codes or params.get("windowsVirtualKeyCode") == key_code


def _typed(params: dict[str, Any], texts: set[str]) -> bool:
    return params.get("text") in texts or params.get("unmodifiedText") in texts


def is_enter_key(params: dict[str, Any]) -> bool:
    return _named(params, ENTER_KEYS, ENTER_CODES, ENTER_KEY_CODE) or _typed(params, ENTER_TEXTS)


def is_space_key(params: dict[str, Any]) -> bool:
    return _named(params, SPACE_KEYS, SPACE_CODES, SPACE_KEY_CODE) or _typed(params, SPACE_TEXTS)


def _activation_key_name(params: dict[str, Any]) -> str | None:
    if is_enter_key(params):
        return "enter"
    if is_space_key(params):
        return "space"
    return None


def _named_by_text_only(params: dict[str, Any]) -> bool:
    """Enter or Space given only as typed text, with no key, code or key code naming it."""
    return not _named(params, ENTER_KEYS, ENTER_CODES, ENTER_KEY_CODE) and not _named(
        params, SPACE_KEYS, SPACE_CODES, SPACE_KEY_CODE
    )


def pressed_key_names(params: dict[str, Any]) -> set[str]:
    """Lower-case names a key event matches in ``trigger.key``: its key and code, plus
    "enter" for Enter and " " and "space" for Space however the event names them."""
    names = {str(params.get(field) or "").casefold() for field in ("key", "code")} - {""}
    if is_enter_key(params):
        names.add("enter")
    if is_space_key(params):
        names.update({" ", "space"})
    return names


def is_pointer_method(method: str) -> bool:
    return method in POINTER_METHODS


def is_activation_key(params: dict[str, Any]) -> bool:
    """Keys that activate a focused button or link (Enter, Space)."""
    return _activation_key_name(params) is not None


def _is_activating_key(method: str, params: dict[str, Any]) -> bool:
    modifiers = params.get("modifiers")
    shortcut = isinstance(modifiers, int) and bool(modifiers & SHORTCUT_MODIFIERS)
    return method == KEY_METHOD and (is_activation_key(params) or shortcut)


def _key_types_alone(params: dict[str, Any]) -> bool:
    """A key event that types Enter or Space by itself: a char not following its key down,
    or a key down named only by its text. It both starts and ends the activation."""
    kind = params.get("type")
    if kind == "char":
        return params.get(FOLLOWS_KEY_DOWN_PARAM) is not True
    return kind in KEY_DOWN_TYPES and _named_by_text_only(params)


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
    return _starts_key_activation(method, params)


def _starts_key_activation(method: str, params: dict[str, Any]) -> bool:
    if not _is_activating_key(method, params):
        return False
    if params.get("type") == "char":
        return _key_types_alone(params)
    return params.get("type") in KEY_DOWN_TYPES


def is_commit(method: str, params: dict[str, Any]) -> bool:
    """Actions after which post-conditions run: mouse or touch release, tap, drop, Enter
    key up, and Enter typed by itself (a lone char, or a key down named only by text)."""
    kind = params.get("type")
    if method in MOUSE_LIKE_METHODS:
        return kind == "mouseReleased"
    if method == TOUCH_METHOD:
        return kind == "touchEnd"
    if method == TAP_GESTURE_METHOD:
        return True
    if method == DRAG_METHOD:
        return kind == "drop"  # the drop replaces the mouse release
    if method != KEY_METHOD or not is_enter_key(params):
        return False
    return kind == "keyUp" or _key_types_alone(params)


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


class KeyPresses:
    """Per CDP session, the Enter or Space key held down by a rawKeyDown (or a keyDown
    without text, which Chromium treats the same), so that the char typing it is not a
    second activation and commit (rawKeyDown + char + keyUp is one key press)."""

    MAX_SESSIONS = 256

    def __init__(self) -> None:
        self._down: dict[str | None, str] = {}

    def annotate(self, action: CdpAction) -> CdpAction:
        """The action with FOLLOWS_KEY_DOWN_PARAM set by Statelock (never taken from the agent)."""
        if action.method != KEY_METHOD:
            return action
        params = {key: value for key, value in action.params.items() if key != FOLLOWS_KEY_DOWN_PARAM}
        kind = params.get("type")
        name = _activation_key_name(params)
        # Any other key event ends the pairing: a later char is a key press of its own.
        down = self._down.pop(action.session_id, None)
        if kind == "char" and name is not None and down == name:
            params[FOLLOWS_KEY_DOWN_PARAM] = True
        elif (
            kind in KEY_DOWN_TYPES
            and name is not None
            and not _named_by_text_only(params)
            and (kind == "rawKeyDown" or not params.get("text"))
        ):
            self._down[action.session_id] = name
            while len(self._down) > self.MAX_SESSIONS:
                self._down.pop(next(iter(self._down)))
        return replace(action, params=params)


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


def classify_cdp_action(payload: dict[str, Any]) -> CdpAction | None:
    """The governed input action a command is, or None when it is not one."""
    method = payload.get("method")
    kind = MUTATING_CDP_METHODS.get(method) if isinstance(method, str) else None
    if kind is None:
        return None
    return _action(payload, str(method), kind)


def protocol_action(payload: dict[str, Any]) -> CdpAction:
    """Wrap any CDP command as a PROTOCOL action (for refused commands)."""
    return _action(payload, str(payload.get("method") or ""), ActionKind.PROTOCOL)


def _action(payload: dict[str, Any], method: str, kind: ActionKind) -> CdpAction:
    message_id = payload.get("id")
    params = payload.get("params")
    session_id = payload.get("sessionId")
    return CdpAction(
        message_id=message_id if isinstance(message_id, int) else None,
        method=method,
        kind=kind,
        params=params if isinstance(params, dict) else {},
        session_id=session_id if isinstance(session_id, str) else None,
    )
