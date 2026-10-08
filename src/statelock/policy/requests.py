# SPDX-License-Identifier: Apache-2.0
"""Which HTTP requests an agent may have Statelock make for it (policy ``request_access``).

Agent-side HTTP with the browser's cookies would act as the logged-in user outside
Statelock, so cookies are not exported (policy ``cookie_access``). Instead the agent
asks Statelock to make the request in the page (``Statelock.fetch``; Playwright's
``page.request`` after ``statelock.client.install()``). Statelock checks it against
the agent's ``request_access``, makes it with the browser's session, and records it.

    request_access:
      - url_pattern: "^https://portal\\.example\\.com/api/"   # regex matched from the start of the URL
        methods: [GET, POST]                                  # default [GET]
        max_response_bytes: 5000000                           # default 8 MB
        # Redirects fail before the destination is requested.

Access is per agent: the union of the agent's policies. Anything else is declined
(rule ``request_access``) and recorded; the session continues.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from statelock.policy.patterns import checked_regex

DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The response travels to the agent base64-encoded in one CDP message (16 MiB limit).
MAX_RESPONSE_BYTES = 11 * 1024 * 1024
ANCHORED_URL_PATTERN = re.compile(r"\^(?:https\??|http)://")
# The pattern's origin: a scheme, a literal host and an optional port, then '/'. A
# wildcard or a bare prefix would let an allowed host match another host such as
# portal.example.com.attacker.test.
PINNED_ORIGIN_PATTERN = re.compile(
    r"\^(?P<scheme>https\?|https|http)://(?P<host>(?:[A-Za-z0-9-]|\\\.)+)(?::(?P<port>[0-9]+|\[0-9\]\+))?/"
)
ANY_PORT = "[0-9]+"
_ESCAPE = re.compile(r"\\.", re.DOTALL)
# A class, with ']' as a literal first member ("[]a]", "[^]a]").
_CHARACTER_CLASS = re.compile(r"\[\^?\]?[^\]]*\]")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class RequestRule(BaseModel):
    """One allowed kind of request.

    A request is allowed only when both hold:

    - the URL begins with the pattern's origin, character for character (case aside):
      ``scheme://host[:port]/``. Checked on the URL text, not on a parsed URL, so
      nothing (user info, a backslash, an encoded or trailing-dot host, a default
      port) can make the browser see another host than the one compared here;
    - the pattern matches from the start of the URL. Alternation at the top level
      (``^https://a\\.test/|evil``) is refused when the policy loads, so no branch
      can drop the origin.

    ``follow_redirects`` exists only to refuse ``true`` with a message that says why
    (a redirect's destination cannot be checked before the browser sends it); extra
    keys would be refused anyway, but with a bare "extra inputs" error.
    """

    model_config = ConfigDict(extra="forbid")

    url_pattern: str = Field(min_length=1)
    methods: list[str] = Field(default_factory=lambda: ["GET"])
    max_response_bytes: int = Field(default=DEFAULT_MAX_RESPONSE_BYTES, gt=0, le=MAX_RESPONSE_BYTES)
    follow_redirects: bool = False
    _schemes: tuple[str, ...] = PrivateAttr(default=())
    _host: str = PrivateAttr(default="")
    _port: str | None = PrivateAttr(default=None)

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
        if not PINNED_ORIGIN_PATTERN.match(value):
            raise ValueError("url_pattern must pin a literal host followed by '/' (with an optional explicit port)")
        checked_regex(value, "url_pattern")
        if _has_top_level_alternation(value):
            raise ValueError("url_pattern must not use '|' outside a group: a branch could match another host")
        return value

    @model_validator(mode="after")
    def _pinned_origin(self) -> RequestRule:
        match = PINNED_ORIGIN_PATTERN.match(self.url_pattern)
        if match is None:  # refused by _anchored_regex already
            raise ValueError("url_pattern must pin a literal host")
        scheme = match.group("scheme")
        self._schemes = ("http", "https") if scheme == "https?" else (scheme,)
        self._host = match.group("host").replace("\\.", ".").lower()
        self._port = match.group("port")
        return self

    @field_validator("methods")
    @classmethod
    def _known_methods(cls, value: list[str]) -> list[str]:
        methods = [method.upper() for method in value]
        unknown = sorted(set(methods) - METHODS)
        if not methods or unknown:
            raise ValueError(f"methods must be some of {sorted(METHODS)}; got {unknown or 'none'}")
        return methods

    def allows(self, method: str, url: str) -> bool:
        return (
            method.upper() in self.methods
            and self._on_pinned_origin(url)
            and re.match(self.url_pattern, url) is not None
        )

    def _on_pinned_origin(self, url: str) -> bool:
        """Whether ``url`` starts with the pinned ``scheme://host[:port]/``, compared as text.
        The URL is already canonical (core.urls.canonical_http_url); this is a second check
        beside the regex, not the only one."""
        for scheme in self._schemes:
            prefix = f"{scheme}://{self._host}"
            head = url[: len(prefix)]
            # ASCII case only: Unicode lowercasing maps other characters onto ASCII letters.
            if not head.isascii() or head.lower() != prefix:
                continue
            rest = url[len(prefix) :]
            if self._port is None:
                return rest.startswith("/")
            port, slash, _ = rest.partition("/")
            if not slash or not port.startswith(":"):
                return False
            digits = port[1:]
            # ASCII digits only: str.isdigit() also accepts other scripts' digits.
            if not digits or not all("0" <= char <= "9" for char in digits):
                return False
            return self._port in {ANY_PORT, digits}
        return False


def _has_top_level_alternation(pattern: str) -> bool:
    """Whether ``pattern`` has a '|' outside any group or character class."""
    bare = _CHARACTER_CLASS.sub("", _ESCAPE.sub("", pattern))
    depth = 0
    for char in bare:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            return True
    return False


class RequestAccess:
    """The union of an agent's request rules."""

    def __init__(self, rules: list[RequestRule]) -> None:
        self.rules = rules

    def rule_for(self, method: str, url: str) -> RequestRule | None:
        """The first rule that allows the request, or None."""
        return next((rule for rule in self.rules if rule.allows(method, url)), None)
