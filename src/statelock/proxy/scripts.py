# SPDX-License-Identifier: Apache-2.0
"""Which tabs are running agent code right now (Runtime.evaluate / callFunctionOn).

A synthetic click the page guard sees while the agent's code runs in that tab (or
just after it, before any input reached the tab) is taken to come from that code,
and is replayed as governed mouse input. A click the site's own code makes while
handling real input is not. Either misreading fails safe: a site click taken for
an agent click is still checked as a real click; an agent click taken for a site
click is blocked.
"""

from __future__ import annotations

import time
from typing import Any

from statelock.core.actions import parse_cdp_message
from statelock.core.jsonutil import as_dict, as_str

SCRIPT_METHODS = {"Runtime.evaluate", "Runtime.callFunctionOn"}
# A script whose response never came (its tab closed, the agent stopped waiting) stops
# counting as running after this. Expiring is safe: its clicks are then blocked, not replayed.
MAX_RUNNING_SECONDS = 60.0

DETACHED_EVENT = "Target.detachedFromTarget"

ScriptKey = tuple[str | None, int]


class AgentScripts:
    def __init__(self, grace: float = 0.5, max_running: float = MAX_RUNNING_SECONDS) -> None:
        self.grace = grace
        self.max_running = max_running
        # (cdp sessionId, id) -> (targetId, monotonic start time)
        self._running: dict[ScriptKey, tuple[str, float]] = {}
        self._finished_at: dict[str, float] = {}  # targetId -> monotonic time
        self._input_at: dict[str, float] = {}  # targetId -> last input dispatched to it

    def started(self, payload: dict[str, Any], target_id: str | None) -> None:
        message_id = payload.get("id")
        if payload.get("method") in SCRIPT_METHODS and target_id is not None and isinstance(message_id, int):
            self._running[(as_str(payload.get("sessionId")), message_id)] = (target_id, time.monotonic())

    def observe(self, raw_message: str | bytes) -> None:
        """Note a browser -> agent message: a running script's response, or a detached session."""
        if not self._running or isinstance(raw_message, bytes):
            return
        detached = DETACHED_EVENT in raw_message
        if not detached and '"id"' not in raw_message:
            return
        payload = parse_cdp_message(raw_message)
        if payload is None:
            return
        if detached and payload.get("method") == DETACHED_EVENT:
            # The tab is gone: its scripts will never answer.
            session_id = as_str(as_dict(payload.get("params")).get("sessionId"))
            for key in [key for key in self._running if key[0] == session_id]:
                del self._running[key]
            return
        message_id = payload.get("id")
        if not isinstance(message_id, int):
            return
        running = self._running.pop((as_str(payload.get("sessionId")), message_id), None)
        if running is not None:
            self._finished_at[running[0]] = time.monotonic()

    def input_dispatched(self, target_id: str | None) -> None:
        """Real input (the agent's, or a replay) is going to this tab."""
        if target_id is not None:
            self._input_at[target_id] = time.monotonic()

    def active(self, target_id: str) -> bool:
        now = time.monotonic()
        for key, (running_target, started) in list(self._running.items()):
            if now - started > self.max_running:
                del self._running[key]
            elif running_target == target_id:
                return True
        finished = self._finished_at.get(target_id)
        if finished is None or time.monotonic() - finished > self.grace:
            return False
        return self._input_at.get(target_id, 0.0) < finished
