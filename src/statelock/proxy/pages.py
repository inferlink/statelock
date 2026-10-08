# SPDX-License-Identifier: Apache-2.0
"""Statelock's own attached CDP session per browser target, reused across captures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from statelock.core.jsonutil import as_dict, as_str
from statelock.proxy.connection import DETACHED_EVENT, CdpConnection

logger = logging.getLogger(__name__)

BASE_DOMAINS = ("Runtime.enable", "Page.enable", "DOM.enable")


class PageSessions:
    """Attach once per target on Statelock's connection and cache the session ID.

    Components that keep state per Statelock session register ``on_detached`` to drop
    it when the session ends (its tab closed or was replaced).
    """

    def __init__(self, connection: CdpConnection) -> None:
        self.connection = connection
        self._sessions: dict[str, str] = {}  # targetId -> Statelock sessionId
        self._targets: dict[str, str] = {}  # Statelock sessionId -> targetId
        self._attaching: dict[str, asyncio.Future[str]] = {}
        self._detach_callbacks: list[Callable[[str], object]] = []
        connection.add_listener(self._on_event)

    def on_detached(self, callback: Callable[[str], object]) -> None:
        """Call ``callback(session_id)`` when one of Statelock's CDP sessions detaches."""
        self._detach_callbacks.append(callback)

    async def session_for(self, target_id: str) -> str:
        existing = self._sessions.get(target_id)
        if existing is not None:
            return existing
        pending = self._attaching.get(target_id)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._attaching[target_id] = future
        try:
            result = await self.connection.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session_id = str(result["sessionId"])
            for method in BASE_DOMAINS:
                await self.connection.send(method, session_id=session_id)
            self._sessions[target_id] = session_id
            self._targets[session_id] = target_id
            future.set_result(session_id)
            return session_id
        except BaseException as error:
            if not future.done():
                future.set_exception(error)
                future.exception()  # mark retrieved; callers get it via await
            raise
        finally:
            self._attaching.pop(target_id, None)

    def target_for_session(self, session_id: str | None) -> str | None:
        """The target of one of Statelock's own sessions."""
        return self._targets.get(session_id or "")

    async def list_targets(self) -> list[dict[str, Any]]:
        result = await self.connection.send("Target.getTargets")
        infos = result.get("targetInfos", [])
        return [info for info in infos if isinstance(info, dict)]

    def _on_event(self, message: dict[str, Any]) -> None:
        if message.get("method") != DETACHED_EVENT:
            return
        session_id = as_str(as_dict(message.get("params")).get("sessionId"))
        if session_id is None:
            return
        target_id = self._targets.pop(session_id, None)
        if target_id is not None:
            self._sessions.pop(target_id, None)
        for callback in list(self._detach_callbacks):
            try:
                callback(session_id)
            except Exception:  # one component's cleanup must not skip the others'
                logger.exception("Detach callback failed for CDP session %s", session_id)
