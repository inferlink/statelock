# SPDX-License-Identifier: Apache-2.0
"""CDP to the governed Chromium: one pipe, shared by Statelock and the agent.

Chromium runs with ``--remote-debugging-pipe``: CDP goes over two file descriptors,
each message a JSON text ended by a NUL byte, and no TCP port exists that a page
could reach. ``CdpMultiplexer`` splits that pipe into two channels that behave
like two separate CDP clients:

- command ids are renumbered on the pipe and restored in each answer;
- a CDP session belongs to the channel that attached it (or whose session
  auto-attached it); a command on the other channel's session is answered
  "not found", and that session's events reach only its owner;
- browser-level events go to the agent, except ``Browser.*`` events (downloads),
  which go to Statelock only.

``CdpConnection`` is Statelock's client on its channel; the agent's commands go
through the other channel as text (``CdpChannel.send`` / ``recv``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from statelock.core.jsonutil import as_dict, as_int, as_str, parse_cdp_message
from statelock.wire import INVALID_REQUEST, PARSE_ERROR

logger = logging.getLogger(__name__)

EventListener = Callable[[dict[str, Any]], None]

MESSAGE_END = b"\0"
# The default for STATELOCK_CDP_COMMAND_TIMEOUT: seconds one of Statelock's own CDP commands may take.
DEFAULT_COMMAND_TIMEOUT = 5.0
ATTACHED_EVENT = "Target.attachedToTarget"
DETACHED_EVENT = "Target.detachedFromTarget"
STATELOCK_EVENT_DOMAIN = "Browser."
# Chromium's own answers for a session that is not the client's.
SESSION_NOT_FOUND = {"code": -32001, "message": "Session with given id not found."}
NO_SESSION_WITH_ID = {"code": -32602, "message": "No session with given id"}


class CdpError(Exception):
    """A CDP command failed: an error response (``code`` is its CDP error code), a timeout or a closed connection."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class BrowserClosedError(Exception):
    """The browser's CDP pipe is closed: nothing can be sent or received any more."""


class CdpPipe:
    """Chromium's end of ``--remote-debugging-pipe``: NUL-terminated JSON messages."""

    def __init__(
        self, reader: asyncio.StreamReader, read_transport: asyncio.ReadTransport, transport: asyncio.WriteTransport
    ) -> None:
        self._reader = reader
        self._read_transport = read_transport
        self._transport = transport

    @classmethod
    async def open(cls, read_fd: int, write_fd: int) -> CdpPipe:
        """Wrap our ends of the two pipes (Chromium writes to read_fd's pipe, reads write_fd's)."""
        loop = asyncio.get_running_loop()
        # No size limit, as with any CDP client: screenshots and page text can be large.
        reader = asyncio.StreamReader(limit=2**31)
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0)
        )
        transport, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(write_fd, "wb", buffering=0))
        return cls(reader, read_transport, transport)

    async def read(self) -> str | None:
        """The next message, or None once Chromium closed its end."""
        try:
            data = await self._reader.readuntil(MESSAGE_END)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        return data[:-1].decode("utf-8", errors="replace")

    def write(self, message: str) -> None:
        """Queue one message (written whole, in call order)."""
        if self._transport.is_closing():
            raise BrowserClosedError("the browser's CDP pipe is closed")
        self._transport.write(message.encode("utf-8") + MESSAGE_END)

    def close(self) -> None:
        self._transport.close()
        self._read_transport.close()  # a pending read then ends (None)


class CdpChannel:
    """One client's view of the browser connection: send commands, receive answers and events."""

    def __init__(self, multiplexer: CdpMultiplexer, name: str) -> None:
        self.name = name
        self._multiplexer = multiplexer
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    def send(self, message: str) -> None:
        """Send one CDP command (JSON text): queued on the pipe at once, so nothing to await.
        Raises BrowserClosedError when the browser is gone."""
        self._multiplexer.send(self, message)

    async def recv(self) -> str:
        """The next answer or event for this channel. Raises BrowserClosedError when the browser is gone."""
        message = await self._inbox.get()
        if message is None:
            self._inbox.put_nowait(None)  # every later recv fails too
            raise BrowserClosedError("the browser's CDP pipe is closed")
        return message

    def _deliver(self, message: str | None) -> None:
        self._inbox.put_nowait(message)


@dataclass(frozen=True)
class _Pending:
    channel: CdpChannel
    message_id: int
    attach_target: str | None  # Target.attachToTarget: the target being attached


class CdpMultiplexer:
    """Two CDP clients, ``statelock`` and ``agent``, on one browser pipe (see the module docstring)."""

    def __init__(self, pipe: CdpPipe) -> None:
        self._pipe = pipe
        self.statelock = CdpChannel(self, "statelock")
        self.agent = CdpChannel(self, "agent")
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._owners: dict[str, CdpChannel] = {}  # CDP sessionId -> the channel that owns it
        # Attach events for a target Statelock is attaching to: held until an attach answer
        # names their session, so each goes to the right channel. sessionId -> (targetId, event).
        self._held: dict[str, tuple[str, str]] = {}
        self._closed = False
        self._reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        self._pipe.close()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        self._shut()

    @property
    def closed(self) -> bool:
        return self._closed

    # Channel -> browser --------------------------------------------------------------

    def send(self, channel: CdpChannel, message: str) -> None:
        if self._closed:
            raise BrowserClosedError("the browser's CDP pipe is closed")
        payload = parse_cdp_message(message)
        if payload is None:
            channel._deliver(
                json.dumps({"id": 0, "error": {"code": PARSE_ERROR, "message": "Message must be a JSON object"}})
            )
            return
        message_id = as_int(payload.get("id"))
        if message_id is None:
            channel._deliver(
                json.dumps({"error": {"code": INVALID_REQUEST, "message": "Message must have integer 'id' property"}})
            )
            return
        refusal = self._session_refusal(channel, payload)
        if refusal is not None:
            channel._deliver(json.dumps({"id": message_id, **refusal}))
            return
        self._next_id += 1
        method = payload.get("method")
        attach_target = (
            as_str(as_dict(payload.get("params")).get("targetId")) if method == "Target.attachToTarget" else None
        )
        self._pending[self._next_id] = _Pending(channel, message_id, attach_target)
        payload["id"] = self._next_id
        try:
            self._pipe.write(json.dumps(payload))
        except BaseException:
            self._pending.pop(self._next_id, None)
            raise

    def _session_refusal(self, channel: CdpChannel, payload: dict[str, Any]) -> dict[str, Any] | None:
        """An error answer when the command names a session of the other channel (or none)."""
        if "sessionId" in payload:
            session_id = payload["sessionId"]
            if not isinstance(session_id, str) or self._owners.get(session_id) is not channel:
                return {"sessionId": session_id, "error": SESSION_NOT_FOUND}
        if payload.get("method") == "Target.detachFromTarget":
            # Without a sessionId, Chromium would pick one of the targets' sessions itself.
            session_id = as_dict(payload.get("params")).get("sessionId")
            if not isinstance(session_id, str) or self._owners.get(session_id) is not channel:
                return {"error": NO_SESSION_WITH_ID}
        return None

    # Browser -> channels -------------------------------------------------------------

    async def _read(self) -> None:
        try:
            while (message := await self._pipe.read()) is not None:
                try:
                    self._dispatch(message)
                except Exception:  # one odd message must not stop the session's CDP
                    logger.exception("Could not route a CDP message from the browser")
        finally:
            self._shut()

    def _shut(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self.statelock._deliver(None)
        self.agent._deliver(None)

    def _dispatch(self, message: str) -> None:
        payload = parse_cdp_message(message)
        if payload is None:
            return
        if isinstance(payload.get("id"), int):
            self._answer(payload)
        elif "sessionId" in payload:
            self._session_event(payload, message)
        else:
            self._browser_event(payload, message)

    def _session_event(self, payload: dict[str, Any], message: str) -> None:
        owner = self._owners.get(str(payload["sessionId"]))
        if owner is None:
            return
        method = payload.get("method")
        inner = as_str(as_dict(payload.get("params")).get("sessionId"))
        if method == ATTACHED_EVENT and inner is not None:
            self._owners[inner] = owner  # auto-attached under the owner's session
        elif method == DETACHED_EVENT and inner is not None:
            self._owners.pop(inner, None)
        owner._deliver(message)

    def _browser_event(self, payload: dict[str, Any], message: str) -> None:
        method = payload.get("method")
        params = as_dict(payload.get("params"))
        inner = as_str(params.get("sessionId"))  # the session an attach/detach event is about
        if isinstance(method, str) and method.startswith(STATELOCK_EVENT_DOMAIN):
            self.statelock._deliver(message)
        elif method == ATTACHED_EVENT and inner is not None:
            target_id = as_str(as_dict(params.get("targetInfo")).get("targetId")) or ""
            if self._statelock_attaching(target_id):
                self._held[inner] = (target_id, message)
            else:
                self._owners[inner] = self.agent
                self.agent._deliver(message)
        elif method == DETACHED_EVENT and inner is not None:
            self._held.pop(inner, None)
            owner = self._owners.pop(inner, None)
            if owner is not None:
                owner._deliver(message)
        elif inner is not None:
            owner = self._owners.get(inner)
            if owner is not None:
                owner._deliver(message)
        else:
            self.agent._deliver(message)

    def _answer(self, payload: dict[str, Any]) -> None:
        pending = self._pending.pop(payload["id"], None)
        if pending is None:
            return
        session_id = as_str(as_dict(payload.get("result")).get("sessionId"))
        if session_id is not None and "error" not in payload:
            self._owners[session_id] = pending.channel
            held = self._held.pop(session_id, None)
            if held is not None:
                pending.channel._deliver(held[1])  # Chromium sends the attach event before the answer
        payload["id"] = pending.message_id
        pending.channel._deliver(json.dumps(payload))
        if pending.attach_target is not None and pending.channel is self.statelock:
            self._release_held(pending.attach_target)

    def _statelock_attaching(self, target_id: str) -> bool:
        return any(p.channel is self.statelock and p.attach_target == target_id for p in self._pending.values())

    def _release_held(self, target_id: str) -> None:
        """Statelock's attaches to the target are answered: other attach events are the agent's."""
        if self._statelock_attaching(target_id):
            return
        for session_id, (held_target, message) in list(self._held.items()):
            if held_target == target_id:
                del self._held[session_id]
                self._owners[session_id] = self.agent
                self.agent._deliver(message)


class CdpConnection:
    """Statelock's own CDP client, on its channel of the browser connection.

    ``send`` returns the command result or raises CdpError. Events (messages
    without an id) are passed to every listener; listeners must not block.
    """

    def __init__(self, channel: CdpChannel, command_timeout: float = DEFAULT_COMMAND_TIMEOUT) -> None:
        self.channel = channel
        self.command_timeout = command_timeout
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: list[EventListener] = []
        # Set when the reader stops: nothing can answer a command any more.
        self._closed = False

    def open(self) -> None:
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        self._closed = True

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
        if self._reader is None or self._closed:
            raise CdpError(f"CDP connection is closed ({method} not sent)")
        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            self.channel.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout=timeout or self.command_timeout)
        except asyncio.TimeoutError as error:
            raise CdpError(f"{method} timed out") from error
        except BrowserClosedError as error:
            raise CdpError(f"CDP connection closed during {method}") from error
        finally:
            self._pending.pop(message_id, None)

    async def _read(self) -> None:
        try:
            while True:
                message = parse_cdp_message(await self.channel.recv())
                if message is not None:
                    self._dispatch(message)
        except BrowserClosedError:
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
