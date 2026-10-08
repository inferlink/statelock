# SPDX-License-Identifier: Apache-2.0
"""Regex options in policies: checked when the policy loads."""

from __future__ import annotations

import re


def checked_regex(value: str | None, option: str) -> str | None:
    """``value`` if it compiles; a ValueError (a validation error at load time) naming ``option`` if not."""
    if value is None:
        return None
    try:
        re.compile(value)
    except re.error as error:
        raise ValueError(f"{option} is not a valid regex: {error}") from error
    return value
