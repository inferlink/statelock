# SPDX-License-Identifier: Apache-2.0
"""Agent and reviewer authentication with API keys.

Each agent presents ``Authorization: Bearer <key>`` when it opens the
Statelock WebSocket, and again when it looks up a violation. Reviewers present
their own key to the review API (statelock.review). The keys file stores only
SHA-256 hashes of the keys:

    agents:
      - agent_id: finance_reconciliation_agent
        tenant: acme                   # optional; recorded with every action
        key_sha256: 3b1f...            # statelock keygen prints this entry
        expires: 2027-01-01T00:00:00Z  # optional
        disabled: false                # optional
    reviewers:
      - reviewer_id: alice
        tenant: acme                   # reviews only this tenant's sessions
        key_sha256: 9c0d...            # statelock keygen --reviewer alice
    auditors:
      - auditor_id: audit-team
        tenant: acme                   # reads only this tenant's evidence (for plugins, e.g. the Enclave)
        key_sha256: 51ab...            # statelock keygen --auditor audit-team

Several entries may name the same principal (key rotation). Each key has one
role: agent keys cannot review or audit, reviewer keys cannot open sessions, and
so on. ``STATELOCK_AUTH_MODE``
is ``api_key`` (default) or ``none`` (local development only: IDs are then
self-asserted).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from statelock.fileio import load_yaml_file

logger = logging.getLogger(__name__)

AuthMode = Literal["api_key", "none"]
KEY_PREFIX = "slk_"
AUTHORIZATION_HEADER = "authorization"
BEARER_PREFIX = "bearer "
DEFAULT_TENANT = "default"
LOCAL_REVIEWER = "local-reviewer"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """A new random API key (256 bits)."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def bearer_token(authorization: str | None) -> str | None:
    """The key from an ``Authorization: Bearer <key>`` header value."""
    if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
        return None
    token = authorization[len(BEARER_PREFIX) :].strip()
    return token or None


class AuthError(Exception):
    """Authentication failed. The message is logged, never sent to the client."""


class KeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str = DEFAULT_TENANT
    key_sha256: str
    expires: datetime | None = None
    disabled: bool = False

    @field_validator("key_sha256")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("key_sha256 must be a 64-character hex SHA-256 digest")
        return value

    @field_validator("expires")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def principal(self) -> str:
        raise NotImplementedError

    def check_usable(self) -> None:
        if self.disabled:
            raise AuthError(f"key for {self.principal} is disabled")
        if self.expires is not None and self.expires <= datetime.now(timezone.utc):
            raise AuthError(f"key for {self.principal} expired at {self.expires.isoformat()}")


class AgentKey(KeyEntry):
    agent_id: str = Field(min_length=1)

    @property
    def principal(self) -> str:
        return self.agent_id


class ReviewerKey(KeyEntry):
    reviewer_id: str = Field(min_length=1)

    @property
    def principal(self) -> str:
        return self.reviewer_id


class AuditorKey(KeyEntry):
    auditor_id: str = Field(min_length=1)

    @property
    def principal(self) -> str:
        return self.auditor_id


class KeysFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: list[AgentKey] = Field(default_factory=list)
    reviewers: list[ReviewerKey] = Field(default_factory=list)
    auditors: list[AuditorKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> KeysFile:
        digests: set[str] = set()
        groups: tuple[list[AgentKey] | list[ReviewerKey] | list[AuditorKey], ...] = (
            self.agents,
            self.reviewers,
            self.auditors,
        )
        for group in groups:
            tenants: dict[str, str] = {}
            for entry in group:
                if entry.key_sha256 in digests:
                    raise ValueError("the same key_sha256 appears twice")
                digests.add(entry.key_sha256)
                if tenants.setdefault(entry.principal, entry.tenant) != entry.tenant:
                    raise ValueError(f"{entry.principal} is listed under two tenants")
        return self


@dataclass(frozen=True)
class Identity:
    """Who a connection or request belongs to."""

    agent_id: str
    tenant: str | None
    authenticated: bool


@dataclass(frozen=True)
class Reviewer:
    """A human reviewer calling the review API."""

    reviewer_id: str
    # None when authentication is off: the reviewer sees every tenant.
    tenant: str | None
    authenticated: bool

    def may_review(self, tenant_id: str | None) -> bool:
        return self.tenant is None or self.tenant == tenant_id


@dataclass(frozen=True)
class Auditor:
    """Someone reading recorded evidence (plugin routes such as the Enclave's)."""

    auditor_id: str
    # None when authentication is off: every tenant.
    tenant: str | None
    authenticated: bool

    def may_read(self, tenant_id: str | None) -> bool:
        return self.tenant is None or self.tenant == tenant_id


_Entry = TypeVar("_Entry", AgentKey, ReviewerKey, AuditorKey)


class Authenticator:
    def __init__(self, mode: AuthMode, keys: KeysFile | None = None) -> None:
        if mode == "api_key" and keys is None:
            raise ValueError(
                "STATELOCK_AUTH_MODE=api_key needs STATELOCK_AUTH_KEYS_FILE. "
                "Create keys with `statelock keygen`, or set STATELOCK_AUTH_MODE=none for local development."
            )
        self.mode = mode
        self._agents = {entry.key_sha256: entry for entry in (keys.agents if keys else [])}
        self._reviewers = {entry.key_sha256: entry for entry in (keys.reviewers if keys else [])}
        self._auditors = {entry.key_sha256: entry for entry in (keys.auditors if keys else [])}
        if mode == "none":
            logger.warning(
                "Statelock authentication is disabled (STATELOCK_AUTH_MODE=none): agent IDs are not verified"
            )

    @classmethod
    def from_settings(cls, mode: AuthMode, keys_file: Path | None) -> Authenticator:
        keys = load_keys_file(keys_file) if keys_file is not None else None
        return cls(mode, keys)

    @property
    def enabled(self) -> bool:
        return self.mode == "api_key"

    def _entry(self, table: dict[str, _Entry], authorization: str | None, role: str) -> _Entry:
        key = bearer_token(authorization)
        if key is None:
            raise AuthError("missing bearer key")
        digest = hash_key(key)
        entry = table.get(digest)
        if entry is None:
            roles = (("agent", self._agents), ("reviewer", self._reviewers), ("auditor", self._auditors))
            other = next((name for name, entries in roles if digest in entries), None)
            raise AuthError(f"{other} key used as {role} key" if other else "unknown key")
        entry.check_usable()
        return entry

    def authenticate(self, claimed_agent_id: str | None, authorization: str | None) -> Identity:
        """Resolve the calling agent. Raises AuthError. In mode none, the claimed agent ID is trusted."""
        if not self.enabled:
            if not claimed_agent_id:
                raise AuthError("no agent id")
            return Identity(agent_id=claimed_agent_id, tenant=None, authenticated=False)
        entry = self._entry(self._agents, authorization, "agent")
        if claimed_agent_id and claimed_agent_id != entry.agent_id:
            raise AuthError(f"key belongs to {entry.agent_id}, not {claimed_agent_id}")
        return Identity(agent_id=entry.agent_id, tenant=entry.tenant, authenticated=True)

    def authenticate_reviewer(self, authorization: str | None, claimed_reviewer_id: str | None = None) -> Reviewer:
        """Resolve the calling reviewer. Raises AuthError. In mode none, anyone may review."""
        if not self.enabled:
            return Reviewer(reviewer_id=claimed_reviewer_id or LOCAL_REVIEWER, tenant=None, authenticated=False)
        entry = self._entry(self._reviewers, authorization, "reviewer")
        return Reviewer(reviewer_id=entry.reviewer_id, tenant=entry.tenant, authenticated=True)

    def authenticate_auditor(self, authorization: str | None) -> Auditor:
        """Resolve an auditor key. Raises AuthError. In mode none, anyone reads everything."""
        if not self.enabled:
            return Auditor(auditor_id="local-auditor", tenant=None, authenticated=False)
        entry = self._entry(self._auditors, authorization, "auditor")
        return Auditor(auditor_id=entry.auditor_id, tenant=entry.tenant, authenticated=True)


def load_keys_file(path: Path) -> KeysFile:
    if not path.is_file():
        raise FileNotFoundError(f"Statelock keys file not found (or not a file): {path}")
    payload = load_yaml_file(path) or {}
    return KeysFile.model_validate(payload)
