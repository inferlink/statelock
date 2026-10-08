# SPDX-License-Identifier: Apache-2.0
"""Statelock's own isolated worlds in the agent's pages, kept out of the agent's reach.

Statelock runs its code in isolated worlds of each page: the page guard (guard.js
and its report binding), state capture (page_state.js) and governed requests
(Statelock.fetch). Page scripts cannot see these worlds. The guard's reports decide
what is a violation and the captures decide what policies see, so the agent's
commands may not name these worlds or the guard's binding, nor run code in their
execution contexts (``StatelockContexts.refusal``).
"""

from __future__ import annotations

from typing import Any

from statelock.core.jsonutil import as_dict, as_int, as_str, parse_cdp_message
from statelock.proxy.connection import CdpError
from statelock.proxy.pages import PageSessions

GUARD_WORLD = "__statelock_guard"
STATE_WORLD = "__statelock_state"
REQUEST_WORLD = "__statelock_request"
WORLD_NAMES = frozenset({GUARD_WORLD, STATE_WORLD, REQUEST_WORLD})
GUARD_BINDING = "__statelockGuardReport"
BINDING_NAMES = frozenset({GUARD_BINDING})

# Command params that name a world, or select an execution context.
WORLD_NAME_PARAMS = ("worldName", "executionContextName")
CONTEXT_ID_PARAMS = ("contextId", "executionContextId")
UNIQUE_CONTEXT_PARAM = "uniqueContextId"

CONTEXT_CREATED = "Runtime.executionContextCreated"
CONTEXT_DESTROYED = "Runtime.executionContextDestroyed"
CONTEXTS_CLEARED = "Runtime.executionContextsCleared"
CONTEXT_EVENTS = frozenset({CONTEXT_CREATED, CONTEXT_DESTROYED, CONTEXTS_CLEARED})
# Chromium's answer for a context that no longer exists (nothing was run).
CONTEXT_NOT_FOUND = "Cannot find context"


def context_gone(error: CdpError) -> bool:
    return CONTEXT_NOT_FOUND in str(error)


class _SeenContexts:
    """Statelock-world execution contexts announced on one CDP stream: key -> {id: uniqueId}."""

    def __init__(self) -> None:
        self.by_key: dict[str | None, dict[int, str | None]] = {}

    def apply(self, key: str | None, method: str, params: dict[str, Any]) -> None:
        if method == CONTEXTS_CLEARED:
            self.by_key.pop(key, None)
        elif method == CONTEXT_DESTROYED:
            context_id = as_int(params.get("executionContextId"))
            if context_id is not None:
                self.by_key.get(key, {}).pop(context_id, None)
        elif method == CONTEXT_CREATED:
            context = as_dict(params.get("context"))
            context_id = as_int(context.get("id"))
            if context.get("name") in WORLD_NAMES and context_id is not None:
                self.by_key.setdefault(key, {})[context_id] = as_str(context.get("uniqueId"))

    def ids(self, key: str | None) -> set[int]:
        return set(self.by_key.get(key, {}))

    def unique_ids(self) -> set[str]:
        return {unique for contexts in self.by_key.values() for unique in contexts.values() if unique}


class StatelockContexts:
    """Knows Statelock's execution contexts and refuses agent commands that reach into them.

    Contexts are learned from both streams: Statelock's own connection (by target)
    and what the browser announces to the agent (by the agent's CDP session), so a
    context is known before the agent can have heard of it.
    """

    def __init__(self, pages: PageSessions) -> None:
        self.pages = pages
        self._own = _SeenContexts()
        self._agent = _SeenContexts()
        pages.connection.add_listener(self._on_statelock_event)

    def _on_statelock_event(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if method not in CONTEXT_EVENTS:
            return
        target_id = self.pages.target_for_session(as_str(message.get("sessionId")))
        if target_id is not None:
            self._own.apply(target_id, method, as_dict(message.get("params")))

    def observe(self, raw_message: str) -> None:
        """Note a browser -> agent message (execution context events)."""
        if "Runtime.executionContext" not in raw_message:
            return
        payload = parse_cdp_message(raw_message)
        method = str((payload or {}).get("method") or "")
        if payload is not None and method in CONTEXT_EVENTS:
            self._agent.apply(as_str(payload.get("sessionId")), method, as_dict(payload.get("params")))

    def refusal(self, payload: dict[str, Any], target_id: str | None) -> str | None:
        """Why an agent command is refused (it reaches into Statelock's worlds), or None."""
        method = str(payload.get("method") or "")
        params = as_dict(payload.get("params"))
        for key in WORLD_NAME_PARAMS:
            if params.get(key) in WORLD_NAMES:
                return f"Blocked {method}: {params[key]} is Statelock's own isolated world."
        if method == "Runtime.addBinding" and params.get("name") in BINDING_NAMES:
            return f"Blocked {method}: {params['name']} is Statelock's page guard binding."
        owned = self._agent.ids(as_str(payload.get("sessionId"))) | self._own.ids(target_id)
        for key in CONTEXT_ID_PARAMS:
            if as_int(params.get(key)) in owned:
                return f"Blocked {method}: execution context {params[key]} is one of Statelock's isolated worlds."
        unique = params.get(UNIQUE_CONTEXT_PARAM)
        if isinstance(unique, str) and unique in self._agent.unique_ids() | self._own.unique_ids():
            return f"Blocked {method}: execution context {unique} is one of Statelock's isolated worlds."
        return None


class IsolatedWorld:
    """One of Statelock's named isolated worlds in each frame Statelock reads (by default
    the target's main frame, whose frame id is the target id).

    Created on first use and reused; a navigation destroys it, and it is created
    again on the next use.
    """

    def __init__(self, pages: PageSessions, name: str) -> None:
        self.pages = pages
        self.name = name
        self._contexts: dict[str, dict[str, int]] = {}  # Statelock sessionId -> {frame id: context id}
        pages.connection.add_listener(self._on_event)
        pages.on_detached(lambda session_id: self._contexts.pop(session_id, None))

    def _on_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = as_dict(message.get("params"))
        session_id = as_str(message.get("sessionId"))
        frames = self._contexts.get(session_id or "")
        if session_id is None or frames is None:
            return
        if method == CONTEXTS_CLEARED:
            del self._contexts[session_id]
        elif method == CONTEXT_DESTROYED:
            gone = params.get("executionContextId")
            for frame_id in [frame for frame, context in frames.items() if context == gone]:
                del frames[frame_id]

    async def evaluate(
        self,
        target_id: str,
        params: dict[str, Any],
        timeout: float | None = None,
        frame_id: str | None = None,
    ) -> dict[str, Any]:
        """Runtime.evaluate in this world of a frame of the target (default: its main frame).
        Raises CdpError.

        Retried once only when the world is gone (nothing ran); never after a timeout
        or any other error, which may come after the code ran.
        """
        return await self._in_world(
            target_id, frame_id, "Runtime.evaluate", params, context_param="contextId", timeout=timeout
        )

    async def resolve_node(self, target_id: str, frame_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
        """DOM.resolveNode into this world of the node's frame. Raises CdpError."""
        return await self._in_world(target_id, frame_id, "DOM.resolveNode", params, context_param="executionContextId")

    async def _in_world(
        self,
        target_id: str,
        frame_id: str | None,
        method: str,
        params: dict[str, Any],
        *,
        context_param: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session_id = await self.pages.session_for(target_id)
        frame = frame_id or target_id
        context_id = self._contexts.get(session_id, {}).get(frame)
        if context_id is not None:
            try:
                return await self._send(session_id, method, {**params, context_param: context_id}, timeout)
            except CdpError as error:
                if not context_gone(error):
                    raise
                self._contexts.get(session_id, {}).pop(frame, None)
        context_id = await self._create(session_id, frame)
        return await self._send(session_id, method, {**params, context_param: context_id}, timeout)

    async def _send(
        self, session_id: str, method: str, params: dict[str, Any], timeout: float | None
    ) -> dict[str, Any]:
        return await self.pages.connection.send(method, params, session_id=session_id, timeout=timeout)

    async def _create(self, session_id: str, frame_id: str) -> int:
        world = await self.pages.connection.send(
            "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": self.name}, session_id=session_id
        )
        context_id = world.get("executionContextId")
        if not isinstance(context_id, int):
            raise CdpError(f"Page.createIsolatedWorld returned no context for {self.name}")
        self._contexts.setdefault(session_id, {})[frame_id] = context_id
        return context_id
