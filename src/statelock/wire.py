# SPDX-License-Identifier: Apache-2.0
"""Wire format shared by the Statelock proxy and client SDK.

Everything here is public API: headers, close codes and the violation marker
are what agents and their tooling depend on.
"""

from __future__ import annotations

import json
from typing import Any

WEBSOCKET_PATH = "/statelock"
AGENT_ID_HEADER = "x-statelock-agent-id"
AGENT_ID_QUERY_PARAM = "agent_id"
SESSION_ID_HEADER = "x-statelock-session-id"
VIOLATION_MARKER = "STATELOCK_POLICY_VIOLATION"
VIOLATION_ENDPOINT_PREFIX = "/violations"
REVIEW_ENDPOINT_PREFIX = "/reviews"
REVIEW_PAGE_PATH = "/review"
# Only read when authentication is off: names the local reviewer.
REVIEWER_ID_HEADER = "x-statelock-reviewer-id"
# CDP methods answered by the proxy itself (never forwarded to Chromium).
STATELOCK_METHOD_PREFIX = "Statelock."
# Answered with {session_id, agent_id}: how an SDK recognizes a browser behind Statelock.
SESSION_COMMAND = "Statelock.session"
# Answered with the response of an HTTP request Statelock makes in the page (policy request_access).
REQUEST_COMMAND = "Statelock.fetch"

# Application-defined WebSocket close codes (4000-4999).
CLOSE_CODE_UNREGISTERED_AGENT = 4401
CLOSE_CODE_PRE_CONDITION = 4403
CLOSE_CODE_INVALID_SESSION_ID = 4409
CLOSE_CODE_POST_CONDITION = 4412

# CDP / JSON-RPC server error code used for in-band violation responses.
CDP_VIOLATION_ERROR_CODE = -32000
# Error code for a malformed or rejected Statelock.* command.
CDP_INVALID_PARAMS_ERROR_CODE = -32602

# WebSocket close reasons are limited to 123 bytes of UTF-8.
MAX_CLOSE_REASON_BYTES = 123

VIOLATION_FIELDS = (
    "violation_type",
    "rule",
    "reason",
    "policy_id",
    "agent_id",
    "session_id",
    "sequence",
)

_SHORT_TYPES = {"pre_condition": "pre", "post_condition": "post"}
_LONG_TYPES = {short: long for long, short in _SHORT_TYPES.items()}


def truncate_utf8(value: str, max_bytes: int = MAX_CLOSE_REASON_BYTES) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def encode_violation(violation: dict[str, Any]) -> str:
    """Render a violation as the CDP error message string."""
    compact = {key: violation.get(key) for key in VIOLATION_FIELDS}
    return f"{VIOLATION_MARKER} {json.dumps(compact, separators=(',', ':'))}"


def encode_close_reason(violation: dict[str, Any]) -> str:
    """Render a compact violation for the WebSocket close reason.

    Playwright retries actions whose CDP commands return errors and reports
    the close reason once the session ends. The full violation is served at
    VIOLATION_ENDPOINT_PREFIX/<session_id>.
    """
    violation_type = violation.get("violation_type") or ""
    compact: dict[str, Any] = {
        "t": _SHORT_TYPES.get(violation_type, violation_type),
        "r": violation.get("rule"),
        "s": violation.get("session_id"),
        "n": violation.get("sequence"),
    }
    reason = f"{VIOLATION_MARKER} {json.dumps(compact, separators=(',', ':'))}"
    if len(reason.encode("utf-8")) > MAX_CLOSE_REASON_BYTES:
        compact.pop("r")
        reason = f"{VIOLATION_MARKER} {json.dumps(compact, separators=(',', ':'))}"
    return truncate_utf8(reason)


def decode_violation(message: str) -> dict[str, Any] | None:
    """Extract a violation from any error text that embeds one (full or compact form)."""
    index = message.find(VIOLATION_MARKER)
    if index < 0:
        return None
    remainder = message[index + len(VIOLATION_MARKER) :].lstrip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(remainder)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if "s" in payload or "t" in payload:
        short_type = str(payload.get("t") or "")
        return {
            "violation_type": _LONG_TYPES.get(short_type, short_type or None),
            "rule": payload.get("r"),
            "session_id": payload.get("s"),
            "sequence": payload.get("n"),
        }
    return payload


def statelock_error(message: str) -> dict[str, Any]:
    """A CDP error body for a command Statelock refuses or answers itself."""
    return {"error": {"code": CDP_INVALID_PARAMS_ERROR_CODE, "message": f"Statelock: {message}"}}


def cdp_response(request: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """A CDP response to a request: its id, its sessionId (if any), and body (result or error)."""
    response: dict[str, Any] = {"id": request.get("id"), **body}
    session_id = request.get("sessionId")
    if isinstance(session_id, str):
        response["sessionId"] = session_id
    return response
