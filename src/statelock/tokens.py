# SPDX-License-Identifier: Apache-2.0
"""Single-use tokens behind session URLs (see statelock.sessions)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from statelock.auth import Identity

TOKEN_PREFIX = "slt_"  # noqa: S105 - a prefix, not a secret


@dataclass(frozen=True)
class PendingSession:
    identity: Identity
    session_id: str
    expires_at: datetime
    saved_session_name: str | None = None
    save_session: bool = False


# Unused session URLs one agent may hold at once: bounds memory if an agent keeps asking.
MAX_PENDING_PER_AGENT = 100


class TooManyPendingSessions(Exception):
    """The agent already holds MAX_PENDING_PER_AGENT unused session URLs."""


class SessionTokens:
    """Single-use session tokens, by SHA-256. Expired tokens are dropped as they are seen."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._pending: dict[str, PendingSession] = {}
        self._now = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        identity: Identity,
        ttl_seconds: float,
        *,
        saved_session_name: str | None = None,
        save_session: bool = False,
    ) -> tuple[str, PendingSession]:
        self._drop_expired()
        pending_for_agent = sum(
            1
            for p in self._pending.values()
            if (p.identity.tenant, p.identity.agent_id) == (identity.tenant, identity.agent_id)
        )
        if pending_for_agent >= MAX_PENDING_PER_AGENT:
            raise TooManyPendingSessions(f"{pending_for_agent} unused session URLs; use or let some expire first")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        pending = PendingSession(
            identity=identity,
            session_id=str(uuid.uuid4()),
            expires_at=self._now() + timedelta(seconds=ttl_seconds),
            saved_session_name=saved_session_name,
            save_session=save_session,
        )
        self._pending[self._digest(token)] = pending
        return token, pending

    def peek(self, token: str) -> PendingSession | None:
        self._drop_expired()
        return self._pending.get(self._digest(token))

    def consume(self, token: str) -> PendingSession | None:
        """The session for a token, once: the token is removed."""
        self._drop_expired()
        return self._pending.pop(self._digest(token), None)

    def _drop_expired(self) -> None:
        now = self._now()
        for digest in [d for d, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[digest]
