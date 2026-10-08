# SPDX-License-Identifier: Apache-2.0
"""Which HTTP requests an agent may have Statelock make for it (policy ``request_access``).

Agent-side HTTP with the browser's cookies would act as the logged-in user outside
Statelock, so cookies are not exported (policy ``cookie_access``). Instead the agent
asks Statelock to make the request in the page (``Statelock.fetch``; Playwright's
``page.request`` after ``statelock.client.install()``). Statelock checks it against
the agent's ``request_access``, makes it with the browser's session, and records it.

    request_access:
      - url_pattern: "^https://portal\\.example\\.com/api/"   # regex searched in the full URL; starts with ^http(s)://
        methods: [GET, POST]                                  # default [GET]
        max_response_bytes: 5000000                           # default 8 MB
        # Redirects fail before the destination is requested.

Access is per agent: the union of the agent's policies. Anything else is declined
(rule ``request_access``) and recorded; the session continues.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from statelock.policy.patterns import checked_regex

DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The response travels to the agent base64-encoded in one CDP message (16 MiB limit).
MAX_RESPONSE_BYTES = 11 * 1024 * 1024
ANCHORED_URL_PATTERN = re.compile(r"\^(?:https\??|http)://")
# Host must be literal and followed by '/'; a wildcard or a bare prefix lets an
# allowed host match another host such as portal.example.com.attacker.test.
PINNED_HOST_PATTERN = re.compile(r"^\^(?:https\?|https|http)://(?:[A-Za-z0-9-]|\\\.)+(?::(?:[0-9]+|\[0-9\]\+))?/")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class RequestRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url_pattern: str = Field(min_length=1)
    methods: list[str] = Field(default_factory=lambda: ["GET"])
    max_response_bytes: int = Field(default=DEFAULT_MAX_RESPONSE_BYTES, gt=0, le=MAX_RESPONSE_BYTES)
    follow_redirects: bool = False

    @model_validator(mode="after")
    def _no_unchecked_redirects(self) -> RequestRule:
        if self.follow_redirects:
            raise ValueError("follow_redirects is not supported: the destination cannot be checked before it is sent")
        return self

    @field_validator("url_pattern")
    @classmethod
    def _anchored_regex(cls, value: str) -> str:
        # Unanchored, a pattern also matches its text in another site's path or query
        # (https://elsewhere.example/?/api/), so it must name the scheme from the start.
        if not ANCHORED_URL_PATTERN.match(value):
            raise ValueError(f"url_pattern must start with ^https:// or ^http:// (or ^https?://), got {value!r}")
        if not PINNED_HOST_PATTERN.match(value):
            raise ValueError("url_pattern must pin a literal host followed by '/' (with an optional explicit port)")
        checked_regex(value, "url_pattern")
        return value

    @field_validator("methods")
    @classmethod
    def _known_methods(cls, value: list[str]) -> list[str]:
        methods = [method.upper() for method in value]
        unknown = sorted(set(methods) - METHODS)
        if not methods or unknown:
            raise ValueError(f"methods must be some of {sorted(METHODS)}; got {unknown or 'none'}")
        return methods

    def allows(self, method: str, url: str) -> bool:
        return method.upper() in self.methods and re.search(self.url_pattern, url) is not None


class RequestAccess:
    """The union of an agent's request rules."""

    def __init__(self, rules: list[RequestRule]) -> None:
        self.rules = rules

    def rule_for(self, method: str, url: str) -> RequestRule | None:
        """The first rule that allows the request, or None."""
        return next((rule for rule in self.rules if rule.allows(method, url)), None)
