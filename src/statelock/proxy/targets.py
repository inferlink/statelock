# SPDX-License-Identifier: Apache-2.0
"""Map the agent's CDP sessions to browser targets and track per-target setup."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from statelock.core.jsonutil import as_dict, as_str, parse_cdp_message
from statelock.proxy.connection import ATTACHED_EVENT, DETACHED_EVENT
from statelock.proxy.tasks import BackgroundTasks

logger = logging.getLogger(__name__)

TRACKED_EVENTS = (ATTACHED_EVENT, DETACHED_EVENT)

# Called with (target_id, targetInfo) when the agent attaches to a target; targetInfo also
# carries the attach event's ``waitingForDebugger`` (the target is paused before running
# any script). Returns an awaitable the agent's commands on that target must wait for, or None.
TargetSetup = Callable[[str, dict[str, Any]], Awaitable[Any] | None]


class TargetRegistry:
    """Learns client sessionId -> targetId from the browser's attach/detach events.

    Setup steps (page guard install, download configuration) run once per target, when
    the agent first attaches to it; commands on that session wait for them
    (``await_ready``) and are refused if a step fails or times out (fail closed). When
    the agent attaches to the same target again, its commands wait for the same setup;
    it runs again only if it did not succeed.
    """

    def __init__(self, ready_timeout: float = 10.0) -> None:
        self.ready_timeout = ready_timeout
        self._targets: dict[str, str] = {}
        self._ready: dict[str, asyncio.Future[Any]] = {}
        self._target_setup: dict[str, asyncio.Future[Any]] = {}  # targetId -> its setup
        self._setup: list[TargetSetup] = []
        self._tasks = BackgroundTasks("target setup")

    def add_setup(self, step: TargetSetup) -> None:
        """Register a setup step. Steps are registered before the session's pumps start."""
        self._setup.append(step)

    def target_for(self, cdp_session_id: str | None) -> str | None:
        return self._targets.get(cdp_session_id or "")

    def track(self, raw_message: str) -> None:
        """Inspect a browser -> client message for Target attach/detach events."""
        # Cheap substring check first: this runs on every browser -> client message.
        if not any(event in raw_message for event in TRACKED_EVENTS):
            return
        payload = parse_cdp_message(raw_message)
        if payload is None:
            return
        params = as_dict(payload.get("params"))
        cdp_session_id = as_str(params.get("sessionId"))
        if cdp_session_id is None:
            return
        method = payload.get("method")
        if method == ATTACHED_EVENT:
            info = {**as_dict(params.get("targetInfo")), "waitingForDebugger": params.get("waitingForDebugger") is True}
            self._on_attached(cdp_session_id, info)
        elif method == DETACHED_EVENT:
            self._on_detached(cdp_session_id)

    def _on_attached(self, cdp_session_id: str, target_info: dict[str, Any]) -> None:
        target_id = as_str(target_info.get("targetId"))
        if target_id is None:
            return
        self._targets[cdp_session_id] = target_id
        if cdp_session_id in self._ready:
            return
        setup = self._target_setup.get(target_id)
        if setup is None or (setup.done() and _setup_problem(setup) is not None):
            pending = [step for step in (run(target_id, target_info) for run in self._setup) if step is not None]
            if not pending:
                return
            setup = self._target_setup[target_id] = self._tasks.spawn(asyncio.gather(*pending))
        if not setup.done():
            self._ready[cdp_session_id] = setup

    def _on_detached(self, cdp_session_id: str) -> None:
        target_id = self._targets.pop(cdp_session_id, None)
        self._ready.pop(cdp_session_id, None)
        if target_id is None or target_id in self._targets.values():
            return  # another agent session still uses the target and its setup
        setup = self._target_setup.get(target_id)
        if setup is not None and not setup.done():
            # Nobody waits for it: a later attach starts it again.
            setup.cancel()
            del self._target_setup[target_id]

    async def await_ready(self, cdp_session_id: str | None) -> str | None:
        """Wait until the target's setup finished. Returns an error or None."""
        if cdp_session_id is None:
            return None
        task = self._ready.get(cdp_session_id)
        if task is None:
            return None
        if not task.done():
            await asyncio.wait({task}, timeout=self.ready_timeout)
        if self._ready.get(cdp_session_id) is not task:
            # The target went away while the command waited (its setup was cancelled on
            # detach). Not a policy question: the browser answers the command itself.
            return None
        problem = _setup_problem(task)
        if problem is None:
            self._ready.pop(cdp_session_id, None)
        return problem

    async def close(self) -> None:
        self._ready.clear()
        await self._tasks.close()


def _setup_problem(task: asyncio.Future[Any]) -> str | None:
    """Why a target's setup did not succeed (None: it did)."""
    if not task.done():
        return "target setup timed out"
    if task.cancelled():
        return "target setup was cancelled"
    error = task.exception()
    if error is not None:
        return str(error) or type(error).__name__
    return None
