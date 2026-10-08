import json

from helpers import key_action, mouse_action

from statelock.core.actions import (
    ActionKind,
    classify_cdp_action,
    is_activation_key,
    parse_cdp_message,
    protocol_action,
)
from statelock.wire import (
    MAX_CLOSE_REASON_BYTES,
    decode_violation,
    encode_close_reason,
    encode_violation,
    truncate_utf8,
)


def test_parse_rejects_invalid_json() -> None:
    assert parse_cdp_message("{nope") is None
    assert parse_cdp_message("[1, 2]") is None


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
