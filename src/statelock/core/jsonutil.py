# SPDX-License-Identifier: Apache-2.0
"""Helpers for untyped CDP JSON."""

from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """Return value if it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def as_str(value: Any) -> str | None:
    """Return value if it is a str, else None."""
    return value if isinstance(value, str) else None
