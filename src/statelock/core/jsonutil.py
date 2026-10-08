# SPDX-License-Identifier: Apache-2.0
"""Helpers for untyped CDP JSON."""

from __future__ import annotations

import json
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """Return value if it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def as_str(value: Any) -> str | None:
    """Return value if it is a str, else None."""
    return value if isinstance(value, str) else None


def as_int(value: Any) -> int | None:
    """Return value if it is an int (not a bool, which JSON true/false parse to), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_cdp_message(raw_message: str) -> dict[str, Any] | None:
    """A CDP message as a dict, or None when it is not a JSON object."""
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
