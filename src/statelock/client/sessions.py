# SPDX-License-Identifier: Apache-2.0
"""Session URLs: open a Statelock session from one URL, without headers.

    url = create_session_url("http://localhost:8010")      # key: STATELOCK_API_KEY
    browser = await playwright.chromium.connect_over_cdp(url.cdp_url)

Any framework that takes a browser URL (Stagehand ``cdp_url``, Puppeteer
``browserURL``, a LangChain or CrewAI browser tool) can use ``cdp_url``. The URL
works once and expires after a few minutes. From a shell: ``statelock session-url``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from statelock.client.violations import StatelockPolicyViolationError

API_KEY_ENV = "STATELOCK_API_KEY"
SERVER_ENV = "STATELOCK_URL"
REQUEST_TIMEOUT = 10.0


class StatelockClientError(Exception):
    """Statelock refused or could not answer a request (session URLs, saved sessions,
    violation lookups, guards). ``status`` is the HTTP status when Statelock answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def resolve_api_key(api_key: str | None, default: str | None = None) -> str | None:
    """The key to send: ``api_key``, else ``default``, else STATELOCK_API_KEY.

    One rule everywhere: None means "not given" (fall back), "" means "send no key"
    and is kept, so a session created with ``api_key=""`` never picks up the
    environment's key for its later lookups either.
    """
    for key in (api_key, default):
        if key is not None:
            return key
    return os.environ.get(API_KEY_ENV)


@dataclass(frozen=True)
class SessionUrl:
    session_id: str
    agent_id: str
    cdp_url: str  # http(s)://.../sessions/<token>: for frameworks that take a browser URL
    ws_url: str  # ws(s)://.../sessions/<token>/devtools: the WebSocket itself
    expires_at: str
    saved_session: str | None = None  # the saved browser session restored into this session
    # The key that created the session; the violation lookup uses it unless given another.
    api_key: str | None = field(default=None, repr=False, compare=False)

    def _key(self, api_key: str | None) -> str | None:
        return resolve_api_key(api_key, self.api_key)

    def guard(self, api_key: str | None = None) -> AbstractAsyncContextManager[None]:
        """Raise StatelockPolicyViolationError (rule, reason) when Statelock ends this session."""
        from statelock.client.violations import statelock_guard  # noqa: PLC0415 - import cycle

        return statelock_guard(self.cdp_url, self.session_id, self._key(api_key))

    def guard_sync(self, api_key: str | None = None) -> AbstractContextManager[None]:
        """``guard`` for Playwright's sync API."""
        from statelock.client.violations import statelock_guard_sync  # noqa: PLC0415 - import cycle

        return statelock_guard_sync(self.cdp_url, self.session_id, self._key(api_key))

    def violation(self, api_key: str | None = None) -> StatelockPolicyViolationError | None:
        """The violation that ended this session, or None if there is none (blocking).

        Raises StatelockClientError when Statelock refuses the lookup (for example a wrong
        key) or cannot be reached: that is not the same as "no violation".
        """
        from statelock.client.violations import StatelockPolicyViolationError, lookup_violation  # noqa: PLC0415

        found = lookup_violation(self.cdp_url, self.session_id, self._key(api_key))
        return StatelockPolicyViolationError(found, recorded=True) if found else None


def create_session_url(
    server_url: str | None = None,
    *,
    api_key: str | None = None,
    agent_id: str | None = None,
    ttl_seconds: int | None = None,
    saved_session: str | None = None,
    save_session: bool = False,
) -> SessionUrl:
    """Ask Statelock for a single-use session URL for the agent the key belongs to.

    server_url defaults to STATELOCK_URL, api_key to STATELOCK_API_KEY. agent_id is
    needed only when the proxy's authentication is off.

    saved_session names a saved browser session: Statelock restores its cookies and
    localStorage before the agent connects (the agent never receives them). With
    save_session, Statelock saves the browser state under that name when the session
    ends cleanly (no violation). The first run of a new name starts empty.
    """
    if save_session and not saved_session:
        raise StatelockClientError("save_session needs saved_session")
    body: dict[str, Any] = {}
    if agent_id:
        body["agent_id"] = agent_id
    if ttl_seconds:
        body["ttl_seconds"] = ttl_seconds
    if saved_session:
        body["saved_session_name"] = saved_session
        body["save_session"] = save_session
    key = resolve_api_key(api_key)
    payload = request_json("POST", "/sessions", server_url, key, body)
    return SessionUrl(
        **{name: str(payload[name]) for name in ("session_id", "agent_id", "cdp_url", "ws_url", "expires_at")},
        saved_session=payload.get("saved_session_name"),
        api_key=key,
    )


def list_saved_sessions(
    server_url: str | None = None, *, api_key: str | None = None, agent_id: str | None = None
) -> list[str]:
    """Names of the calling agent's saved browser sessions."""
    query = f"?agent_id={urllib.parse.quote(agent_id)}" if agent_id else ""
    payload = request_json("GET", f"/saved-sessions{query}", server_url, api_key)
    return [str(name) for name in payload.get("saved_sessions") or []]


def delete_saved_session(
    name: str, server_url: str | None = None, *, api_key: str | None = None, agent_id: str | None = None
) -> bool:
    """Delete one of the calling agent's saved browser sessions. False if it did not exist."""
    query = f"?agent_id={urllib.parse.quote(agent_id)}" if agent_id else ""
    payload = request_json("DELETE", f"/saved-sessions/{urllib.parse.quote(name)}{query}", server_url, api_key)
    return bool(payload.get("deleted"))


def request_json(
    method: str,
    path: str,
    server_url: str | None,
    api_key: str | None,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """One JSON request to the Statelock server (``server_url`` or STATELOCK_URL); the
    client's shared transport. Any failure raises StatelockClientError (with the status)."""
    server = (server_url or os.environ.get(SERVER_ENV) or "").rstrip("/")
    if not server:
        raise StatelockClientError("no Statelock server URL (pass server_url or set STATELOCK_URL)")
    if urllib.parse.urlsplit(server).scheme not in {"http", "https"}:
        raise StatelockClientError(f"not an http(s) Statelock URL: {server}")
    key = resolve_api_key(api_key)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"  # "" sends none
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - http(s) checked above
        f"{server}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - http(s) checked above
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise StatelockClientError(f"Statelock refused the request ({error.code}): {detail}", error.code) from error
    except OSError as error:  # URLError, a timeout, a refused connection
        raise StatelockClientError(f"could not reach Statelock at {server}: {error}") from error
    except ValueError as error:  # not JSON
        raise StatelockClientError(f"Statelock at {server} did not answer with JSON") from error
    return payload if isinstance(payload, dict) else {}
