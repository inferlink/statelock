# SPDX-License-Identifier: Apache-2.0
"""Session URLs: a Statelock session an agent framework opens from one URL, no headers.

An agent's key creates the session (``POST /sessions``, or ``statelock session-url``).
The answer holds a URL with a single-use token in its path, which works where a
framework takes a browser URL:

- ``cdp_url`` (``http://host/sessions/<token>``) for frameworks that look up the
  browser WebSocket at ``<url>/json/version`` (Playwright ``connect_over_cdp``,
  Puppeteer ``browserURL``, Stagehand ``cdp_url``);
- ``ws_url`` (``ws://host/sessions/<token>/devtools``) for frameworks that take the
  WebSocket itself.

The token identifies the agent and the session; it is used up when the WebSocket
opens and expires after ``STATELOCK_SESSION_URL_TTL`` (300 s). Statelock keeps only
its SHA-256, and redacts tokens from its own and uvicorn's logs.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from statelock.auth import Identity
from statelock.dependencies import authenticated_identity
from statelock.saved_sessions import NOT_CONFIGURED, SavedSessionError, SavedSessionKey
from statelock.services import get_services
from statelock.settings import MAX_SESSION_URL_TTL
from statelock.tokens import TOKEN_PREFIX, TooManyPendingSessions

logger = logging.getLogger(__name__)

SESSIONS_PATH = "/sessions"
TOKEN_RE = re.compile(r"slt_[A-Za-z0-9_-]+")


def redact_tokens(text: str) -> str:
    return TOKEN_RE.sub(TOKEN_PREFIX + "[redacted]", text)


def _redact_record(record: logging.LogRecord) -> None:
    if isinstance(record.msg, str):
        record.msg = redact_tokens(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(redact_tokens(arg) if isinstance(arg, str) else arg for arg in record.args)


def install_log_redaction() -> None:
    """Keep session-URL tokens (the URL path carries one) out of every log record.

    A logger's filters do not see its children's records, and handlers belong to the
    host, so the redaction is in the log record factory: it covers uvicorn's access
    log, every ``statelock.*`` logger and any other library.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, "statelock_redacts_tokens", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        _redact_record(record)
        return record

    factory.statelock_redacts_tokens = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Only with authentication off: the agent the session is for (else the key decides).
    agent_id: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=MAX_SESSION_URL_TTL)
    saved_session_name: str | None = Field(default=None, min_length=1, max_length=80)
    save_session: bool = False

    @model_validator(mode="after")
    def _saved_session_is_named(self) -> SessionRequest:
        if self.save_session and not self.saved_session_name:
            raise ValueError("save_session needs saved_session_name")
        return self


router = APIRouter()


def _urls(request: Request, token: str) -> tuple[str, str]:
    base = str(request.base_url).rstrip("/")
    ws_base = "wss" + base[len("https") :] if base.startswith("https") else "ws" + base[len("http") :]
    return f"{base}{SESSIONS_PATH}/{token}", f"{ws_base}{SESSIONS_PATH}/{token}/devtools"


def _resolve_identity(identity: Identity | None, agent_id: str | None) -> Identity:
    """The caller's agent: the key's, or (authentication off) the self-asserted ``agent_id``."""
    if identity is None:
        if not agent_id:
            raise HTTPException(status_code=422, detail="agent_id is required when authentication is off")
        return Identity(agent_id=agent_id, tenant=None, authenticated=False)
    if agent_id and agent_id != identity.agent_id:
        raise HTTPException(status_code=403, detail="the key belongs to another agent")
    return identity


def _saved_session_key(identity: Identity | None, agent_id: str | None) -> SavedSessionKey:
    resolved = _resolve_identity(identity, agent_id)
    return SavedSessionKey(tenant_id=resolved.tenant, agent_id=resolved.agent_id, name="")


@router.post(SESSIONS_PATH)
async def create_session(
    body: SessionRequest, request: Request, identity: Annotated[Identity | None, Depends(authenticated_identity)]
) -> dict[str, Any]:
    services = get_services(request.app)
    identity = _resolve_identity(identity, body.agent_id)  # authentication off: self-asserted, as on the WebSocket
    if not services.evaluator.is_registered(identity.agent_id):
        raise HTTPException(status_code=403, detail=f"agent not registered: {identity.agent_id}")
    ttl = body.ttl_seconds or services.settings.session_url_ttl
    if body.saved_session_name:
        try:
            SavedSessionKey(identity.tenant, identity.agent_id, body.saved_session_name).validate()
        except SavedSessionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not services.saved_sessions.enabled:
            raise HTTPException(status_code=422, detail=NOT_CONFIGURED)
    try:
        token, pending = services.tokens.issue(
            identity, ttl, saved_session_name=body.saved_session_name, save_session=body.save_session
        )
    except TooManyPendingSessions as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    cdp_url, ws_url = _urls(request, token)
    logger.info("Issued session URL session=%s agent_id=%s", pending.session_id, identity.agent_id)
    return {
        "session_id": pending.session_id,
        "agent_id": identity.agent_id,
        "saved_session_name": pending.saved_session_name,
        "save_session": pending.save_session,
        "cdp_url": cdp_url,
        "ws_url": ws_url,
        "expires_at": pending.expires_at.isoformat(),
    }


@router.get("/saved-sessions")
async def list_saved_sessions(
    request: Request,
    identity: Annotated[Identity | None, Depends(authenticated_identity)],
    agent_id: str | None = None,
) -> dict[str, Any]:
    key = _saved_session_key(identity, agent_id)
    return {
        "agent_id": key.agent_id,
        "tenant_id": key.tenant_id,
        "saved_sessions": get_services(request.app).saved_sessions.names(
            tenant_id=key.tenant_id, agent_id=key.agent_id
        ),
    }


@router.delete("/saved-sessions/{name}")
async def delete_saved_session(
    name: str,
    request: Request,
    identity: Annotated[Identity | None, Depends(authenticated_identity)],
    agent_id: str | None = None,
) -> dict[str, Any]:
    key = _saved_session_key(identity, agent_id)
    target = SavedSessionKey(key.tenant_id, key.agent_id, name)
    try:
        deleted = get_services(request.app).saved_sessions.delete(target)
    except SavedSessionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"agent_id": target.agent_id, "tenant_id": target.tenant_id, "name": name, "deleted": deleted}


@router.get(SESSIONS_PATH + "/{token}/json/version")
@router.get(SESSIONS_PATH + "/{token}/json/version/")
async def session_version(token: str, request: Request) -> dict[str, Any]:
    """The DevTools discovery answer frameworks read before opening the WebSocket."""
    if get_services(request.app).tokens.peek(token) is None:
        raise HTTPException(status_code=404, detail="unknown or expired session URL")
    return {
        "Browser": "Statelock",
        "Protocol-Version": "1.3",
        "webSocketDebuggerUrl": _urls(request, token)[1],
    }
