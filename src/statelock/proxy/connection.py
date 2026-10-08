# SPDX-License-Identifier: Apache-2.0
"""A CDP client connection with request futures and event dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets

from statelock.core.actions import parse_cdp_message
from statelock.core.jsonutil import as_dict

logger = logging.getLogger(__name__)

EventListener = Callable[[dict[str, Any]], None]


class CdpError(Exception):
    """A CDP command failed: an error response (``code`` is its CDP error code), a timeout or a closed connection."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CdpConnection:
    """One WebSocket to the browser's CDP endpoint.

    ``send`` returns the command result or raises CdpError. Events (messages
    without an id) are passed to every listener; listeners must not block.
    """

    def __init__(self, ws_url: str, command_timeout: float = 5.0) -> None:
        self.ws_url = ws_url
        self.command_timeout = command_timeout
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: list[EventListener] = []
        # Set when the reader stops: nothing can answer a command any more.
        self._closed = False

    async def open(self) -> None:
        self._ws = await websockets.connect(self.ws_url, max_size=None)
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: EventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._ws is None or self._closed:
            raise CdpError(f"CDP connection is closed ({method} not sent)")
        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout=timeout or self.command_timeout)
        except asyncio.TimeoutError as error:
            raise CdpError(f"{method} timed out") from error
        except websockets.ConnectionClosed as error:
            raise CdpError(f"CDP connection closed during {method}") from error
        finally:
            self._pending.pop(message_id, None)

    async def _read(self) -> None:
        try:
            async for raw_message in self._ws:
                message = parse_cdp_message(raw_message) if isinstance(raw_message, str) else None
                if message is not None:
                    self._dispatch(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("CDP connection closed"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, int):
            self._resolve(message_id, message)
            return
        for listener in list(self._listeners):
            try:
                listener(message)
            except Exception:  # a faulty listener must not stop the reader
                logger.exception("CDP event listener failed")

    def _resolve(self, message_id: int, message: dict[str, Any]) -> None:
        future = self._pending.get(message_id)
        if future is None or future.done():
            return
        if "error" in message:
            error = as_dict(message.get("error"))
            code = error.get("code")
            text = str(error.get("message") or message["error"])
            future.set_exception(CdpError(text, code if isinstance(code, int) else None))
        else:
            future.set_result(as_dict(message.get("result")))
