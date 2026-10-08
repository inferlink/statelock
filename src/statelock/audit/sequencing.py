# SPDX-License-Identifier: Apache-2.0
"""Write action records strictly in sequence order."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from statelock import events as event_names
from statelock.audit.records import ActionRecord
from statelock.audit.sink import ArtifactSink
from statelock.events import Events


class SequencedWriter:
    """Hands out action sequence numbers and writes records strictly in that order.

    Sequence numbers are allocated without holding a lock across capture or
    evaluation; writes wait for their turn so sinks (and sink wrappers such as a hash
    chain) always receive records in order. A slot that is never written is
    released, which leaves a visible gap in the sequence. Bookkeeping is
    synchronous so a cancelled task cannot stall later writers.
    """

    def __init__(
        self,
        sink: ArtifactSink,
        events: Events,
        scrub: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.sink = sink
        self.events = events
        self._allocated = 0
        self._next_to_write = 1
        self._finished: set[int] = set()
        self._waiters: dict[int, asyncio.Future[None]] = {}
        # Applied to each record's payload before it is stored (e.g. removing injected secrets).
        self.scrub = scrub

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[Slot]:
        self._allocated += 1
        slot = Slot(self, self._allocated)
        try:
            yield slot
        finally:
            if not slot.done:
                self._finish(slot.sequence)

    async def _wait_turn(self, sequence: int) -> None:
        if self._next_to_write >= sequence:
            return
        future = self._waiters.get(sequence)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._waiters[sequence] = future
        await future

    def _finish(self, sequence: int) -> None:
        self._finished.add(sequence)
        while self._next_to_write in self._finished:
            self._finished.discard(self._next_to_write)
            self._next_to_write += 1
        waiter = self._waiters.pop(self._next_to_write, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def _write(self, slot: Slot, record: ActionRecord) -> str:
        await self._wait_turn(slot.sequence)
        if self.scrub is not None:
            record = record.model_copy(update={"payload": self.scrub(record.payload)})
        try:
            location = await self.sink.write_action(record)
        finally:
            slot.done = True
            self._finish(slot.sequence)
        await self.events.emit(event_names.ACTION_WRITTEN, {"record": record, "location": location})
        return location


class Slot:
    def __init__(self, writer: SequencedWriter, sequence: int) -> None:
        self.writer = writer
        self.sequence = sequence
        self.done = False

    async def write(self, record: ActionRecord) -> str:
        return await self.writer._write(self, record)
