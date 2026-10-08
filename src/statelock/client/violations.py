# SPDX-License-Identifier: Apache-2.0
"""Violation errors and the lookup of a session's violation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError

from statelock.client.sessions import StatelockClientError, request_json
from statelock.wire import VIOLATION_ENDPOINT_PREFIX, VIOLATION_MARKER, decode_violation

LOOKUP_TIMEOUT = 2.0
# Playwright reports a session Statelock closed as TargetClosedError (or this text).
CLOSED_ERROR_NAME = "TargetClosedError"
CLOSED_ERROR_TEXT = "has been closed"


class StatelockPolicyViolationError(Exception):
    """Raised when Statelock blocks an action or a post-condition fails."""

    def __init__(self, violation: dict[str, Any], *, recorded: bool = False) -> None:
        self.violation = violation
        # True when ``violation`` is the record Statelock keeps (GET /violations): guards
        # do not look it up again.
        self.recorded = recorded
        self.violation_type: str | None = violation.get("violation_type")
        self.rule: str | None = violation.get("rule")
        self.reason: str = violation.get("reason") or "Statelock policy violation"
        self.policy_id: str | None = violation.get("policy_id")
        self.agent_id: str | None = violation.get("agent_id")
        self.session_id: str | None = violation.get("session_id")
        self.sequence: int | None = violation.get("sequence")
        super().__init__(
            f"[{self.violation_type}/{self.rule}] {self.reason} "
            f"(policy={self.policy_id} session={self.session_id} action={self.sequence})"
        )


def _server(endpoint_url: str) -> str:
    """The Statelock server of a WebSocket endpoint or session URL."""
    parts = urlsplit(endpoint_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def lookup_violation(
    endpoint_url: str, session_id: str, api_key: str | None, timeout: float = LOOKUP_TIMEOUT
) -> dict[str, Any] | None:
    """The violation Statelock recorded for the session, or None (404: there is none).

    Blocking. Any other failure (a wrong key, a server error, no answer) raises
    StatelockClientError: it does not mean the session had no violation.
    """
    path = f"{VIOLATION_ENDPOINT_PREFIX}/{quote(session_id, safe='')}"
    try:
        return request_json("GET", path, _server(endpoint_url), api_key, timeout=timeout) or None
    except StatelockClientError as error:
        if error.status == 404:
            return None
        raise


def _may_be_violation(error: BaseException) -> bool:
    """Only a closed session or the violation marker can mean Statelock ended the session."""
    text = str(error)
    return type(error).__name__ == CLOSED_ERROR_NAME or CLOSED_ERROR_TEXT in text or VIOLATION_MARKER in text


def _violation_for(
    error: BaseException, endpoint_url: str | None, session_id: str | None, api_key: str | None
) -> StatelockPolicyViolationError | None:
    """The violation behind an error that may be one: the marker, completed by the lookup."""
    violation = decode_violation(str(error)) or {}
    lookup_id = session_id or violation.get("session_id")
    record = lookup_violation(endpoint_url, lookup_id, api_key) if endpoint_url and isinstance(lookup_id, str) else None
    if record:
        return StatelockPolicyViolationError({**violation, **record}, recorded=True)
    return StatelockPolicyViolationError(violation) if violation else None


def complete_violation(
    error: StatelockPolicyViolationError, endpoint_url: str | None, session_id: str | None, api_key: str | None
) -> StatelockPolicyViolationError:
    """An SDK-raised violation (a blocked download) with the record Statelock keeps for it (blocking).

    A violation that already is the record is not looked up again. When the lookup
    fails or finds nothing, the known violation is returned unchanged: it is still the
    reason the session ended, and a lookup error must not replace it.
    """
    lookup_id = session_id or error.session_id
    if error.recorded or not endpoint_url or not lookup_id:
        return error
    try:
        details = lookup_violation(endpoint_url, lookup_id, api_key)
    except StatelockClientError:
        return error
    return StatelockPolicyViolationError({**error.violation, **details}, recorded=True) if details else error


@asynccontextmanager
async def statelock_guard(
    endpoint_url: str | None = None,
    session_id: str | None = None,
    api_key: str | None = None,
) -> AsyncIterator[None]:
    """Turn errors caused by a Statelock violation into StatelockPolicyViolationError.

    Playwright reports a terminated session as a generic TargetClosedError, so
    when endpoint_url and session_id are known the guard asks the proxy whether
    the session ended in a violation, then falls back to a marker in the error
    text. A StatelockPolicyViolationError raised by the SDK (a blocked download)
    is completed with the recorded violation, unless it already is the record; if
    that lookup fails, the known violation is raised as it is. Other Playwright
    errors (timeouts, missing elements) pass through unchanged, without a lookup.
    A failed lookup for a closed session raises StatelockClientError.
    """
    try:
        yield
    except StatelockPolicyViolationError as error:
        completed = await asyncio.to_thread(complete_violation, error, endpoint_url, session_id, api_key)
        if completed is error:
            raise
        raise completed from error
    except PlaywrightError as error:
        if not _may_be_violation(error):
            raise
        violation = await asyncio.to_thread(_violation_for, error, endpoint_url, session_id, api_key)
        if violation is None:
            raise
        raise violation from error


@contextmanager
def statelock_guard_sync(
    endpoint_url: str | None = None,
    session_id: str | None = None,
    api_key: str | None = None,
) -> Iterator[None]:
    """``statelock_guard`` for Playwright's sync API (the same Error class)."""
    try:
        yield
    except StatelockPolicyViolationError as error:
        completed = complete_violation(error, endpoint_url, session_id, api_key)
        if completed is error:
            raise
        raise completed from error
    except PlaywrightError as error:
        if not _may_be_violation(error):
            raise
        violation = _violation_for(error, endpoint_url, session_id, api_key)
        if violation is None:
            raise
        raise violation from error
