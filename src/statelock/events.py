# SPDX-License-Identifier: Apache-2.0
"""In-process event hooks for plugins.

Events:
- VIOLATION: a violation ended a session. Payload: the violation dict.
- ACTION_WRITTEN: an action record was stored. Payload: {"record": ActionRecord, "location": str}.
- REQUEST_RECORDED: a guarded request was logged. Payload: the network record dict.
- REVIEW_REQUESTED: an action is paused for a reviewer. Payload: the review summary dict.
- REVIEW_DECIDED: a review was approved, denied, expired or cancelled. Payload: the review summary dict.
- SESSION_CLOSED: a session ended and all its records are written. Payload: {"session_id", "agent_id",
  "tenant_id", "violation": bool, "error": bool}.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

VIOLATION = "violation"
ACTION_WRITTEN = "action_written"
REQUEST_RECORDED = "request_recorded"
REVIEW_REQUESTED = "review_requested"
REVIEW_DECIDED = "review_decided"
SESSION_CLOSED = "session_closed"

EventHandler = Callable[[Any], Awaitable[None]]


class Events:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, name: str, handler: EventHandler) -> None:
        self._handlers[name].append(handler)

    async def emit(self, name: str, payload: Any) -> None:
        for handler in list(self._handlers.get(name, ())):
            try:
                await handler(payload)
            except Exception:
                logger.exception("Statelock event handler failed for %s", name)
