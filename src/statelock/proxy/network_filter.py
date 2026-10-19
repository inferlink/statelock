# SPDX-License-Identifier: Apache-2.0
"""Remove credential-bearing headers from CDP traffic sent to an agent."""

from __future__ import annotations

import json
from typing import Any

from statelock.audit.redaction import REDACTED

SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-xsrf-token",
}
# Cookie lists stay lists: CDP clients such as Playwright iterate them.
COOKIE_LISTS = {"associatedCookies", "blockedCookies", "exemptedCookies"}
# Optional raw header text is dropped rather than nulled, as CDP omits it when absent.
RAW_HEADER_TEXT = {"headersText", "requestHeadersText", "responseHeadersText"}


def _clean(value: Any, key: str = "") -> Any:
    if key in COOKIE_LISTS:
        return []
    # Header values must stay strings: Playwright splits every value and fails on null.
    if key.casefold() in SENSITIVE_HEADERS:
        return REDACTED
    if isinstance(value, dict):
        return {name: _clean(item, name) for name, item in value.items() if name not in RAW_HEADER_TEXT}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def filter_network_message(raw: str) -> str:
    """Remove cookies, authorization headers and raw header text from a Network.* event.
    Any other message is returned unchanged, byte for byte."""
    if '"Network.' not in raw:
        return raw
    # Messages reach here already parsed once by the multiplexer, which drops anything else.
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not str(payload.get("method", "")).startswith("Network."):
        return raw
    return json.dumps(_clean(payload))
