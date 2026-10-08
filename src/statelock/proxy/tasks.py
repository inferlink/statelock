# SPDX-License-Identifier: Apache-2.0
"""Tracked background tasks for one session component."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BackgroundTasks:
    """Tasks a component starts from event callbacks: tracked, logged on failure, closed together."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, awaitable: Awaitable[T]) -> asyncio.Task[T]:
        task = asyncio.ensure_future(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("%s background task failed", self.owner, exc_info=task.exception())

    def __len__(self) -> int:
        return len(self._tasks)

    async def close(self, drain_timeout: float = 0.0) -> None:
        """Let running tasks finish for up to drain_timeout seconds, then cancel the rest."""
        if self._tasks and drain_timeout > 0:
            await asyncio.wait(set(self._tasks), timeout=drain_timeout)
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
