import json

import pytest
from helpers import key_action, mouse_action

from statelock.core.actions import (
    FOLLOWS_KEY_DOWN_PARAM,
    ActionKind,
    KeyPresses,
    classify_cdp_action,
    is_activation_key,
    is_enter_key,
    pressed_key_names,
    protocol_action,
)
from statelock.core.jsonutil import as_int, parse_cdp_message
from statelock.wire import (
    MAX_CLOSE_REASON_BYTES,
    STATELOCK_ERROR_CODE,
    decode_violation,
    encode_close_reason,
    encode_violation,
    statelock_error,
    truncate_utf8,
)


def test_parse_rejects_invalid_json() -> None:
    assert parse_cdp_message("{nope") is None
    assert parse_cdp_message("[1, 2]") is None


def test_as_int_takes_ints_but_not_booleans() -> None:
    assert as_int(3) == 3
    assert as_int(True) is None
    assert as_int(3.0) is None
    assert as_int("3") is None


def test_every_statelock_refusal_carries_the_statelock_error_code() -> None:
    error = statelock_error("declined")["error"]
    assert error == {"code": STATELOCK_ERROR_CODE, "message": "Statelock: declined"}


def test_classify_mouse_and_ignore_other_methods() -> None:
    action = classify_cdp_action(
        {"id": 7, "method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed"}, "sessionId": "S"}
    )
    assert action is not None
    assert action.kind == ActionKind.MOUSE
    assert action.message_id == 7
    assert action.session_id == "S"
    assert classify_cdp_action({"id": 1, "method": "Runtime.evaluate"}) is None


def test_commit_actions() -> None:
    assert mouse_action("mouseReleased").is_commit
    assert not mouse_action("mouseMoved").is_commit
    assert key_action("keyUp").is_commit
    assert not key_action("keyDown").is_commit
    assert not key_action("keyUp", key="a", code="KeyA").is_commit


def test_activation_keys() -> None:
    assert is_activation_key({"key": "Enter"})
    assert is_activation_key({"code": "Space"})
    assert is_activation_key({"windowsVirtualKeyCode": 13})
    assert not is_activation_key({"key": "a", "code": "KeyA"})
    char_enter = _classify("Input.dispatchKeyEvent", type="char", text="\r")
    assert char_enter.starts_activation and char_enter.is_commit


@pytest.mark.parametrize("kind", ["keyDown", "rawKeyDown", "char", "keyUp"])
def test_enter_and_space_are_recognised_however_named(kind: str) -> None:
    for params in ({"key": "Enter"}, {"code": "NumpadEnter"}, {"windowsVirtualKeyCode": 13}, {"text": "\r"}):
        assert is_enter_key({"type": kind, **params}), params
    assert is_enter_key({"type": kind, "unmodifiedText": "\n"})
    for params in ({"key": " "}, {"code": "Space"}, {"windowsVirtualKeyCode": 32}, {"text": " "}):
        assert is_activation_key({"type": kind, **params}) and not is_enter_key({"type": kind, **params})
    assert is_activation_key({"type": kind, "unmodifiedText": " "})
    assert not is_activation_key({"type": kind, "key": "a", "code": "KeyA", "text": "a"})


def _press(*events: dict) -> list[tuple[bool, bool]]:
    """(starts activation, commits) of each key event, in one CDP session."""
    keys = KeyPresses()
    outcome = []
    for params in events:
        action = keys.annotate(_classify("Input.dispatchKeyEvent", **params))
        outcome.append((action.starts_activation, action.is_commit))
    return outcome


ENTER = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}


def test_each_key_press_is_one_activation_and_one_commit() -> None:
    # Playwright: keyDown with text, keyUp.
    assert _press({"type": "keyDown", **ENTER, "text": "\r"}, {"type": "keyUp", **ENTER}) == [
        (True, False),
        (False, True),
    ]
    # rawKeyDown + char + keyUp is one press: the char is part of it.
    assert _press({"type": "rawKeyDown", **ENTER}, {"type": "char", "text": "\r"}, {"type": "keyUp", **ENTER}) == [
        (True, False),
        (False, False),
        (False, True),
    ]
    # Typed by itself: a lone char, or a key down named only by its text, is both.
    assert _press({"type": "char", "text": "\r"}) == [(True, True)]
    assert _press({"type": "char", "unmodifiedText": "\r"}) == [(True, True)]
    assert _press({"type": "keyDown", "text": "\r"}, {"type": "keyUp"}) == [(True, True), (False, False)]
    # Space activates (no post-conditions), also as a char.
    assert _press({"type": "char", "text": " "}) == [(True, False)]
    assert _press({"type": "rawKeyDown", "key": " ", "code": "Space"}, {"type": "char", "text": " "}) == [
        (True, False),
        (False, False),
    ]
    # A second char is a second key press.
    assert _press({"type": "rawKeyDown", **ENTER}, {"type": "char", "text": "\r"}, {"type": "char", "text": "\r"})[
        2
    ] == (True, True)


def test_key_pairing_is_statelock_s_own() -> None:
    # The agent cannot mark a char as part of an earlier key press.
    keys = KeyPresses()
    forged = keys.annotate(
        _classify("Input.dispatchKeyEvent", type="char", text="\r", **{FOLLOWS_KEY_DOWN_PARAM: True})
    )
    assert FOLLOWS_KEY_DOWN_PARAM not in forged.params and forged.starts_activation and forged.is_commit
    # Pairing is per CDP session.
    keys.annotate(
        classify_cdp_action(
            {"method": "Input.dispatchKeyEvent", "sessionId": "A", "params": {"type": "rawKeyDown", **ENTER}}
        )
    )
    other = keys.annotate(
        classify_cdp_action(
            {"method": "Input.dispatchKeyEvent", "sessionId": "B", "params": {"type": "char", "text": "\r"}}
        )
    )
    assert other.is_commit


def test_trigger_key_names() -> None:
    assert "enter" in pressed_key_names({"type": "keyDown", "text": "\r"})
    assert "enter" in pressed_key_names({"type": "char", "unmodifiedText": "\r"})
    assert {"space", " "} <= pressed_key_names({"type": "char", "text": " "})
    assert pressed_key_names({"type": "keyDown", "key": "a", "code": "KeyA"}) == {"a", "keya"}


def test_protocol_action_wraps_any_command() -> None:
    action = protocol_action({"id": 3, "method": "Target.sendMessageToTarget", "params": {"x": 1}})
    assert action.kind == ActionKind.PROTOCOL
    assert action.message_id == 3
    assert action.params == {"x": 1}


def test_encode_decode_roundtrip_inside_error_text() -> None:
    encoded = encode_violation({"rule": "assert_field_equal", "reason": "mismatch", "sequence": 3})
    message = f"Page.click: Protocol error (Input.dispatchMouseEvent): {encoded}\nCall log:\n  - x"
    decoded = decode_violation(message)
    assert decoded is not None
    assert decoded["rule"] == "assert_field_equal"
    assert decoded["sequence"] == 3
    assert decode_violation("Target closed") is None


def test_close_reason_fits_and_decodes() -> None:
    violation = {
        "violation_type": "post_condition",
        "rule": "prohibit_click_text",
        "session_id": "196d239c-ee20-4d0e-8d09-cdbc06452748",
        "sequence": 99999,
    }
    reason = encode_close_reason(violation)
    assert len(reason.encode("utf-8")) <= MAX_CLOSE_REASON_BYTES
    assert decode_violation(f"Browser logs:\n\n{reason}\n") == violation


def test_truncate_utf8_keeps_valid_text() -> None:
    value = "é" * 100
    truncated = truncate_utf8(value, 11)
    assert len(truncated.encode("utf-8")) <= 11
    json.dumps(truncated)


def _classify(method: str, **params):
    action = classify_cdp_action({"id": 3, "method": method, "params": params})
    assert action is not None, method
    return action


def test_classify_drag_ime_gestures_and_file_upload() -> None:
    assert _classify("Input.dispatchDragEvent", type="dragOver", x=1, y=2).kind == ActionKind.DRAG
    assert _classify("Input.imeSetComposition", text="a").kind == ActionKind.TEXT
    assert _classify("Input.synthesizeTapGesture", x=1, y=2).kind == ActionKind.TOUCH
    assert _classify("Input.synthesizeScrollGesture", x=1, y=2).kind == ActionKind.TOUCH
    assert _classify("Input.synthesizePinchGesture", x=1, y=2, scaleFactor=2).kind == ActionKind.TOUCH
    assert _classify("Input.emulateTouchFromMouseEvent", type="mousePressed").kind == ActionKind.MOUSE
    assert _classify("DOM.setFileInputFiles", files=["/uploads/a.pdf"], nodeId=4).kind == ActionKind.FILE_UPLOAD


def test_commit_for_drop_tap_and_emulated_touch_release() -> None:
    assert _classify("Input.dispatchDragEvent", type="drop", x=1, y=2).is_commit
    assert not _classify("Input.dispatchDragEvent", type="dragOver", x=1, y=2).is_commit
    assert _classify("Input.synthesizeTapGesture", x=1, y=2).is_commit
    assert _classify("Input.emulateTouchFromMouseEvent", type="mouseReleased").is_commit
    assert not _classify("Input.synthesizeScrollGesture", x=1, y=2).is_commit
    assert not _classify("DOM.setFileInputFiles", files=[]).is_commit
