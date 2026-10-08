# SPDX-License-Identifier: Apache-2.0
"""Remove credential-bearing headers from CDP traffic sent to an agent."""

from __future__ import annotations

import json
from typing import Any

SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization"}
COOKIE_DETAIL_KEYS = {"associatedCookies", "blockedCookies", "exemptedCookies", "cookie"}
REDACTED = "[REDACTED]"


def _clean(value: Any, key: str = "") -> Any:
    if key in COOKIE_DETAIL_KEYS:
        return None
    if key.casefold() in SENSITIVE_HEADERS:
        return REDACTED
    if key in {"headersText", "requestHeadersText", "responseHeadersText"}:
        return None
    if isinstance(value, dict):
        return {name: _clean(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def filter_network_message(raw: str) -> str:
    """Preserve non-network CDP messages byte-for-byte."""
    if '"Network.' not in raw:
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict) or not str(payload.get("method", "")).startswith("Network."):
        return raw
    return json.dumps(_clean(payload))
