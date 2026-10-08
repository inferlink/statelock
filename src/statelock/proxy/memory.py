# SPDX-License-Identifier: Apache-2.0
"""Keep remembered policy fields current: page loads and pages the agent leaves."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from statelock.core.jsonutil import as_str
from statelock.policy.fields import SessionMemory
from statelock.proxy.inspector import Inspector
from statelock.proxy.tasks import BackgroundTasks

logger = logging.getLogger(__name__)

MEMORY_TARGET_TYPES = {"page", "iframe"}


class FieldMemoryWatcher:
    """Updates a session's SessionMemory from captures outside governed actions.

    Governed actions update ``memory`` themselves. This adds the
    captures no action triggers: when a page finishes loading (and again after
    ``settle`` seconds, for values rendered later) and before the agent navigates
    away (``remember_page``).
    """

    def __init__(self, session_id: str, memory: SessionMemory, inspector: Inspector, settle: float) -> None:
        self.session_id = session_id
        self.memory = memory
        self.inspector = inspector
        self.settle = settle
        self._tasks = BackgroundTasks("field memory")

    def setup_target(self, target_id: str, target_info: dict[str, Any]) -> None:
        """TargetRegistry setup step: attach Statelock's session so the tab's page loads are seen."""
        if self.memory.enabled and target_info.get("type") in MEMORY_TARGET_TYPES:
            self._tasks.spawn(self.inspector.pages.session_for(target_id))

    def on_browser_event(self, message: dict[str, Any]) -> None:
        """Statelock connection listener: remember fields when a page finishes loading."""
        if not self.memory.enabled or message.get("method") != "Page.loadEventFired":
            return
        target_id = self.inspector.pages.target_for_session(as_str(message.get("sessionId")))
        if target_id is not None:
            self._tasks.spawn(self._remember_after_load(target_id))

    async def _remember_after_load(self, target_id: str) -> None:
        await self.remember_page(target_id, "page_load")
        await asyncio.sleep(self.settle)
        await self.remember_page(target_id, "page_load")

    async def remember_page(self, target_id: str | None, source: str) -> None:
        """Remember matching fields from a tab (page load, before the agent navigates away)."""
        if not self.memory.enabled or target_id is None:
            return
        state = await self.inspector.capture_fields(target_id)
        names = self.memory.update(state, source)
        if names:
            logger.info("Remembered %s from %s (%s) session=%s", names, state and state.url, source, self.session_id)

    async def close(self) -> None:
        await self._tasks.close()
