# SPDX-License-Identifier: Apache-2.0
"""Redaction applied to every artifact payload before it is written."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
# Not part of a longer identifier: a UUID, a hex digest, a path or a file name.
_BEFORE = r"(?<![0-9A-Za-z/_.-])"
_AFTER = r"(?![0-9A-Za-z/_-]|\.\w)"
# US SSN: 123-45-6789, 123 45 6789 or 123456789, without the never-issued 000/666/9xx
# areas, 00 groups and 0000 serials.
SSN_PATTERN = re.compile(_BEFORE + r"(?!000|666|9)\d{3}([- ]?)(?!00)\d{2}\1(?!0000)\d{4}" + _AFTER)
# Card-like numbers: 12 to 19 digits, plain or grouped as printed on cards (4-4-4-x, or
# 4-6-5 / 4-6-4) with spaces or dashes; only those that pass the Luhn check are redacted.
CARD_PATTERN = re.compile(
    _BEFORE + r"(?:\d{12,19}|\d{4}(?P<s>[ -])\d{4}(?P=s)\d{4}(?P=s)\d{1,7}|\d{4}(?P<t>[ -])\d{6}(?P=t)\d{4,5})" + _AFTER
)
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _redact_card(match: re.Match[str]) -> str:
    digits = re.sub(r"[ -]", "", match.group())
    return REDACTED if luhn_valid(digits) else match.group()


def redact_text(text: str) -> str:
    text = CARD_PATTERN.sub(_redact_card, text)
    text = SSN_PATTERN.sub(REDACTED, text)
    return EMAIL_PATTERN.sub(REDACTED, text)


OMITTED_LARGE_FIELDS = {"dom_snapshot", "accessibility_tree"}

# Identifier fields are never pattern-redacted. A UUID group of 12 digits would
# otherwise match the card-number pattern and corrupt session identity.
IDENTIFIER_FIELDS = {
    "session_id",
    "cdp_session_id",
    "target_id",
    "screenshot_file",
    "guid",
    "path",
    "sha256",
    "uploadId",
    "browserContextId",
    "frameId",
}


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_value(payload)
    if not isinstance(redacted, dict):  # pragma: no cover - dict in, dict out
        raise TypeError("redaction must preserve the payload mapping")
    return redacted


def _redact_value(value: Any, key: str | None = None) -> Any:
    if key in OMITTED_LARGE_FIELDS:
        return {"redacted": True, "reason": "large_capture_payload_omitted"}
    if isinstance(value, dict):
        return {item_key: _redact_value(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        if key in IDENTIFIER_FIELDS:
            return value
        return redact_text(value)
    return value


# Text input params that reveal what was typed (Input.insertText, imeSetComposition,
# dispatchKeyEvent). Named keys such as Enter or Tab are kept.
SECRET_TEXT_PARAMS = ("text", "unmodifiedText")
SECRET_KEY_PARAMS = ("key", "code", "windowsVirtualKeyCode", "nativeVirtualKeyCode", "keyIdentifier")
SECRET_MARKER = "[SECRET]"  # noqa: S105 - a placeholder, not a password


def _is_character_key(params: dict[str, Any]) -> bool:
    key = params.get("key")
    text = params.get("text")
    return (isinstance(key, str) and len(key) == 1) or (isinstance(text, str) and bool(text))


def mask_secret_input(params: dict[str, Any], *, secret_target: bool) -> dict[str, Any]:
    """Params for the record when the focused element is a secret field (password, OTP, card).

    Returns a copy; the command sent to the browser is not changed.
    """
    if not secret_target:
        return params
    masked = dict(params)
    changed = False
    for name in SECRET_TEXT_PARAMS:
        if name in masked:
            masked[name] = SECRET_MARKER
            changed = True
    if _is_character_key(params):
        for name in SECRET_KEY_PARAMS:
            if name in masked:
                masked[name] = SECRET_MARKER
                changed = True
    if changed:
        masked["statelock_redacted"] = "secret_input"
    return masked
