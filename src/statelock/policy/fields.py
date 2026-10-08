# SPDX-License-Identifier: Apache-2.0
"""Named page fields and the per-session memory of remembered values.

A policy's ``fields`` entries name values on a page, so rules can use them
without the site adding ``data-statelock-key`` attributes:

    fields:
      - name: bank_deposit
        url_contains: /bank/          # in scheme://host/path; default: the policy's target_url_contains
        selector: "#deposit-amount"   # CSS; or key: <a data-statelock-key>; or from_url: true
        pattern: "\\$([0-9,.]+)"      # optional; group 1 (or the whole match) is the value
        remember: true                # keep the latest value for the session

Where a value comes from (exactly one):

- ``selector``: the first element matching a CSS selector; its text or input value,
  or ``attribute``. Options: ``frame`` (a CSS selector of a same-origin iframe to
  look inside, e.g. a TinyMCE editor), ``visible: true`` (only elements that are
  shown), and ``read``: ``text`` (default), ``count`` (how many elements match: "0"
  when none, so rules can require an element or its absence) or ``length`` (the
  number of characters of the value; the only thing read from a password field).
- ``key``: an element with ``data-statelock-key``. A policy field's own name is
  never read from that markup: a field that is out of scope or not found is missing.
- ``from_url: true``: the page URL (use ``pattern`` to take a part, e.g. an id).

Rules refer to a field on the current page by name, and to a remembered
value as ``remembered.<name>``. Values are remembered whenever Statelock
captures a matching page: at each governed action (before and after), when a
page finishes loading, and before the agent navigates away (goto, reload,
back/forward).
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from statelock.core.state import BrowserState
from statelock.core.urls import origin_and_path, url_in_scope

REMEMBERED_PREFIX = "remembered."
FIELD_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


FieldRead = Literal["text", "count", "length"]


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=FIELD_NAME_PATTERN)
    selector: str | None = None
    key: str | None = None
    from_url: bool = False
    attribute: str | None = None
    frame: str | None = None
    visible: bool = False
    read: FieldRead = "text"
    pattern: str | None = None
    url_contains: str | None = None
    remember: bool = False
    allowed_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> FieldSpec:
        for origin in self.allowed_origins:
            parsed = origin_and_path(origin)
            if parsed is None or parsed[0] != origin or parsed[1] not in {"", "/"} or "@" in origin:
                raise ValueError(f"field {self.name}: allowed_origins must contain exact http(s) origins")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"field {self.name}: allowed_origins must contain exact http(s) origins")
        if sum((self.selector is not None, self.key is not None, self.from_url)) != 1:
            raise ValueError(f"field {self.name}: set exactly one of selector, key or from_url")
        selector_only = {"attribute": self.attribute, "frame": self.frame, "visible": self.visible or None}
        needs_selector = sorted(name for name, value in selector_only.items() if value is not None)
        if self.read != "text":
            needs_selector.append("read")
        if needs_selector and self.selector is None:
            raise ValueError(f"field {self.name}: {', '.join(needs_selector)} requires selector")
        if self.pattern is not None:
            try:
                compiled = re.compile(self.pattern)
            except re.error as error:
                raise ValueError(f"field {self.name}: invalid pattern: {error}") from error
            if compiled.groups > 1:
                raise ValueError(f"field {self.name}: pattern may have at most one group")
        return self


@dataclass(frozen=True)
class ScopedField:
    """A field spec with its effective URL scope (None: every page)."""

    spec: FieldSpec
    url_contains: str | None

    def applies(self, url: str | None) -> bool:
        if self.spec.allowed_origins:
            parsed = origin_and_path(url) if url else None
            if parsed is None or parsed[0] not in self.spec.allowed_origins:
                return False
        return self.url_contains is None or url_in_scope(self.url_contains, url)


def selector_specs(fields: list[ScopedField]) -> list[dict[str, Any]]:
    """What page_state.js needs to read selector fields (it checks the URL scope itself)."""
    return [
        {
            "name": field.spec.name,
            "selector": field.spec.selector,
            "attribute": field.spec.attribute,
            "frame": field.spec.frame,
            "visible": field.spec.visible,
            "read": field.spec.read,
            "url_contains": field.url_contains,
        }
        for field in fields
        if field.spec.selector is not None
    ]


def apply_pattern(raw: str, pattern: str | None) -> str | None:
    if pattern is None:
        return raw
    match = re.search(pattern, raw)
    if match is None:
        return None
    return (match.group(1) if match.re.groups else match.group(0)).strip()


def resolve_fields(
    fields: list[ScopedField],
    url: str | None,
    key_values: dict[str, Any],
    selector_values: dict[str, Any],
) -> dict[str, Any]:
    """extracted_fields: the data-statelock-key values, plus the named fields for this URL.

    A policy field's name is never filled from page markup: when the field is out of
    scope or not found, it is missing, even if the page has a data-statelock-key of
    that name (the page must not supply a value the policy reads elsewhere).
    """
    policy_names = {field.spec.name for field in fields}
    result = {key: value for key, value in key_values.items() if key not in policy_names}
    for field in fields:
        if not field.applies(url):
            continue
        spec = field.spec
        if spec.from_url:
            raw = url
        elif spec.key is not None:
            raw = key_values.get(spec.key)
        else:
            raw = selector_values.get(spec.name)
        if raw is None or not str(raw).strip():
            continue
        value = apply_pattern(str(raw).strip(), spec.pattern)
        if value:
            result[spec.name] = value
    return result


# A candidate number in text: a digit run with commas and a decimal part.
_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
# What a number token must look like: commas only as thousands separators.
_NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_CURRENCY = "¤"  # every currency symbol (Unicode category Sc) is mapped to this one
_SIGNS = "+-"
# The amount around the number: "(", a sign right before the currency or the digits,
# the currency (a symbol, which may be followed by the sign, or an ISO code such as
# USD), the number, a currency after it and ")".
_AMOUNT = re.compile(
    r"(?P<open>\(\s*)?"
    rf"(?P<sign>[{_SIGNS}](?=[{_CURRENCY}\d]|[A-Z]{{3}}\b))?"
    rf"(?:{_CURRENCY}\s*(?P<inner_sign>[{_SIGNS}](?=\d))?|\b[A-Z]{{3}}\b\s*)?"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    rf"(?:\s*(?:{_CURRENCY}|\b[A-Z]{{3}}\b))?"
    r"(?P<close>\s*\))?"
)


def _normalize_number_text(text: str) -> str:
    """NFKC (full-width digits and parentheses), the Unicode minus as "-", one currency symbol."""
    text = unicodedata.normalize("NFKC", text).replace("\u2212", "-")
    return "".join(_CURRENCY if unicodedata.category(ch) == "Sc" else ch for ch in text).strip()


def parse_number(value: Any) -> float | None:
    """Parse an amount such as "$5,000.00", "USD 1200", "-$5", "$-5", "\u22125" or "(1,234.00)" (negative).

    The text must contain exactly one number: "Qty 2 x $500" or "Invoice #12 $5,000.00"
    return None, so a comparison fails closed instead of checking the wrong amount.
    Commas are accepted only as thousands separators ("5.000,00" returns None).
    Anything else that makes the sign unclear returns None: a trailing sign ("5-"), a
    sign apart from the amount ("- 5"), a hyphen ("INV-5"), two signs, a sign inside
    parentheses, a parenthesis without its pair, or parentheses that do not enclose
    the whole text ("Fee ($5)").
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = _normalize_number_text(str(value))
    token = _single_number(text)
    if token is None:
        return None
    amount = next((m for m in _AMOUNT.finditer(text) if m.start("number") == token.start()), None)
    negative = _is_negative(text, amount) if amount is not None else None
    if negative is None:
        return None
    number = float(token.group().replace(",", ""))
    if not math.isfinite(number):
        return None
    return -number if negative else number


def _single_number(text: str) -> re.Match[str] | None:
    """The one well-formed number in text, or None when there are none, several, or a stray
    decimal part ("5.000,00", ".5")."""
    tokens = list(_NUMBER_TOKEN.finditer(text))
    if len(tokens) != 1 or not _NUMBER.fullmatch(tokens[0].group()):
        return None
    token = tokens[0]
    rest = text[: token.start()] + text[token.end() :]
    if re.search(r"[.,]\d", rest) or text[: token.start()].endswith((".", ",")):
        return None
    return token


def _is_negative(text: str, amount: re.Match[str]) -> bool | None:
    """Whether the amount is negative; None when its sign is unclear."""
    sign, inner_sign = amount.group("sign"), amount.group("inner_sign")
    opened, closed = bool(amount.group("open")), bool(amount.group("close"))
    before, after = text[: amount.start()], text[amount.end() :]
    whole_text = not before and not after
    unclear = (
        bool(sign and inner_sign)  # "-$-5"
        or bool((sign or inner_sign) and (before[-1:].isalnum() or opened))  # "INV-5" is a hyphen; "(-5)"
        or _is_sign_or_dash(before.rstrip()[-1:])  # "- 5", or a dash that is not a minus
        or _is_sign_or_dash(after.lstrip()[:1])  # "5-"
        or (opened != closed)  # "(5", "5)"
        or (opened and not whole_text)  # "Fee ($5)"
    )
    if unclear:
        return None
    return (opened and closed) or "-" in (sign, inner_sign)


def _is_sign_or_dash(char: str) -> bool:
    return bool(char) and (char in _SIGNS or unicodedata.category(char) == "Pd")


class RememberedValue(BaseModel):
    value: str
    url: str | None = None
    source: str
    sequence: int | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionMemory:
    """Latest value of each ``remember: true`` field in one session."""

    def __init__(self, fields: list[ScopedField]) -> None:
        self.fields = [field for field in fields if field.spec.remember]
        self._values: dict[str, RememberedValue] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.fields)

    def update(self, state: BrowserState | None, source: str, sequence: int | None = None) -> list[str]:
        """Remember matching fields from a captured state. Returns the names updated."""
        if state is None or state.capture_error:
            return []
        updated = []
        for field in self.fields:
            if not field.applies(state.url):
                continue
            value = state.extracted_fields.get(field.spec.name)
            if value is None or not str(value).strip():
                continue
            self._values[field.spec.name] = RememberedValue(
                value=str(value).strip(), url=state.url, source=source, sequence=sequence
            )
            updated.append(field.spec.name)
        return updated

    def snapshot(self) -> dict[str, Any]:
        return {name: entry.model_dump(mode="json") for name, entry in sorted(self._values.items())}
