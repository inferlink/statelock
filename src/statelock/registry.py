# SPDX-License-Identifier: Apache-2.0
"""Server-side lookup of the violation that ended a session."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class ViolationRegistry:
    """Bounded in-memory map of session_id -> violation, served at /violations/<id>."""

    def __init__(self, max_entries: int = 1000) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def record(self, violation: dict[str, Any]) -> None:
        session_id = violation.get("session_id")
        if not isinstance(session_id, str):
            return
        self._entries[session_id] = violation
        self._entries.move_to_end(session_id)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._entries.get(session_id)
