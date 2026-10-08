# SPDX-License-Identifier: Apache-2.0
"""Credential injection: the agent types a placeholder, Statelock types the secret.

The agent fills a login field with ``{{secret:<name>}}`` (Playwright ``fill``, or
any framework that sends ``Input.insertText`` or ``Input.imeSetComposition``).
After the action passes its pre-conditions, Statelock replaces the placeholder
with the value on its way to the browser, but only where the secrets file allows it:

    # STATELOCK_SECRETS_FILE
    secrets:
      - name: ojs_password
        agents: [ojs_screening_agent]            # who may use it
        url_contains: https://journal.example.org/index.php/journal/login   # origin, then the path or below it
        value_env: OJS_PASSWORD                  # or value_file: /run/secrets/ojs_password
        # password_fields_only: true             # default: only into password fields

The agent, its LLM prompts and the evidence only see the placeholder. Values are
read at startup (a missing one fails there). Statelock removes injected values
from everything the browser sends the agent and from the evidence. A placeholder
that is not allowed where it is typed ends the session (rule ``secret_injection``).

Limits: typing the placeholder key by key (``keyboard.type``) sends it literally;
agent code running in the page could read a field's value and transform it
before returning it, which no filter can recognise.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from statelock.audit.redaction import SECRET_MARKER
from statelock.core.urls import origin_and_path, url_in_scope
from statelock.fileio import load_yaml_file

PLACEHOLDER_RE = re.compile(r"\{\{secret:([A-Za-z0-9_.-]+)\}\}")


class SecretEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    agents: list[str] = Field(min_length=1)
    # Where it may be typed: the page's origin must equal this one's, and its path must
    # be this path or continue it after a "/". Without the origin, any host would do.
    url_contains: str = Field(min_length=1)
    value_env: str | None = None
    value_file: Path | None = None
    password_fields_only: bool = True

    @field_validator("url_contains")
    @classmethod
    def _origin_scope(cls, value: str) -> str:
        if _scope(value) is None:
            raise ValueError(
                f"url_contains must start with the origin, as https://host[:port]/path, got {value!r} "
                "(no user info, query or fragment)"
            )
        return value

    @model_validator(mode="after")
    def _one_source(self) -> SecretEntry:
        if (self.value_env is None) == (self.value_file is None):
            raise ValueError(f"secret {self.name}: set exactly one of value_env or value_file")
        return self

    def load_value(self) -> str:
        if self.value_env is not None:
            value = os.environ.get(self.value_env)
            if not value:
                raise ValueError(f"secret {self.name}: environment variable {self.value_env} is not set")
            return value
        if self.value_file is None:  # excluded by _one_source
            raise ValueError(f"secret {self.name}: no value source")
        value = self.value_file.read_text(encoding="utf-8").rstrip("\r\n")
        if not value:
            raise ValueError(f"secret {self.name}: {self.value_file} is empty")
        return value


class SecretsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secrets: list[SecretEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> SecretsFile:
        names = [entry.name for entry in self.secrets]
        if len(names) != len(set(names)):
            raise ValueError("a secret name appears twice")
        return self


@dataclass(frozen=True)
class Secret:
    entry: SecretEntry
    value: str

    def __repr__(self) -> str:  # never show the value
        return f"Secret({self.entry.name!r})"


class InjectionError(Exception):
    """A placeholder where its secret may not be typed. The message names the secret, never its value."""


class SecretStore:
    def __init__(self, secrets: list[Secret] | None = None) -> None:
        self._by_name = {secret.entry.name: secret for secret in secrets or []}

    @classmethod
    def load(cls, path: Path) -> SecretStore:
        if not path.is_file():
            raise FileNotFoundError(f"Statelock secrets file not found (or not a file): {path}")
        parsed = SecretsFile.model_validate(load_yaml_file(path) or {})
        return cls([Secret(entry, entry.load_value()) for entry in parsed.secrets])

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)

    @staticmethod
    def placeholders(text: str) -> list[str]:
        return PLACEHOLDER_RE.findall(text)

    def inject(
        self, text: str, *, agent_id: str | None, url: str | None, password_field: bool
    ) -> tuple[str, list[str]]:
        """Replace every placeholder in text. Returns (text, secret names). Raises InjectionError."""
        names = self.placeholders(text)
        for name in names:
            secret = self._by_name.get(name)
            if secret is None:
                raise InjectionError(f"unknown secret {name}")
            entry = secret.entry
            if agent_id not in entry.agents:
                raise InjectionError(f"agent {agent_id} may not use secret {name}")
            if not url or not _in_scope(entry.url_contains, url):
                raise InjectionError(f"secret {name} may only be typed on pages at {entry.url_contains}")
            if entry.password_fields_only and not password_field:
                raise InjectionError(f"secret {name} may only be typed into a password field")
        injected = PLACEHOLDER_RE.sub(lambda match: self._by_name[match.group(1)].value, text)
        return injected, sorted(set(names))

    def value(self, name: str) -> str:
        return self._by_name[name].value


def _scope(url_contains: str) -> tuple[str, str] | None:
    """(origin, path) of a secret's url_contains; None unless it is an http(s) origin with an optional path."""
    try:
        parts = urlsplit(url_contains)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
        return None
    if parts.query or parts.fragment or "?" in url_contains or "#" in url_contains:
        return None
    return origin_and_path(url_contains)


def _in_scope(url_contains: str, url: str) -> bool:
    """The page's origin is the scope's and its path is the scope's path or continues it
    after a "/". Query and fragment are never matched (whoever builds the link picks them)."""
    return _scope(url_contains) is not None and url_in_scope(url_contains, url)


class SecretScrubber:
    """Replaces the secrets a session injected with ``[SECRET]`` in text going to the agent or the evidence."""

    def __init__(self) -> None:
        self._needles: list[str] = []

    def add(self, value: str) -> None:
        # The value as typed, and as it appears inside JSON strings (escaped, ASCII-only or not).
        forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
        for form in forms:
            if form and form not in self._needles:
                self._needles.append(form)
        self._needles.sort(key=len, reverse=True)  # longest first

    def bytes(self, data: bytes) -> bytes:
        """Scrub raw bytes (for example a response body), matching the values' UTF-8 forms."""
        for needle in self._needles:
            encoded = needle.encode("utf-8")
            if encoded in data:
                data = data.replace(encoded, SECRET_MARKER.encode("ascii"))
        return data

    def text(self, text: str) -> str:
        for needle in self._needles:
            if needle in text:
                text = text.replace(needle, SECRET_MARKER)
        return text

    def data(self, value: Any) -> Any:
        """Scrub every string (keys included) in a JSON-like structure."""
        if not self._needles:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.data(key): self.data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.data(item) for item in value]
        return value
