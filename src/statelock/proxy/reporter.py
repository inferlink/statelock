# SPDX-License-Identifier: Apache-2.0
"""Deliver violations to the agent and end the session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from statelock import events as event_names
from statelock.core.actions import CdpAction, parse_cdp_message
from statelock.core.jsonutil import as_str
from statelock.core.state import ActionContext
from statelock.core.verdict import PolicyVerdict
from statelock.events import Events
from statelock.registry import ViolationRegistry
from statelock.wire import (
    CDP_VIOLATION_ERROR_CODE,
    cdp_response,
    encode_close_reason,
    encode_violation,
    truncate_utf8,
)

logger = logging.getLogger(__name__)


def build_violation(context: ActionContext, verdict: PolicyVerdict) -> dict[str, Any]:
    review = verdict.evidence.get("review")
    return {
        "violation_type": verdict.violation_type.value if verdict.violation_type else None,
        "rule": verdict.rule,
        "reason": verdict.reason,
        "policy_id": verdict.policy_id,
        "agent_id": context.agent_id,
        "tenant_id": context.tenant_id,
        "session_id": context.session_id,
        "sequence": context.sequence,
        # Set when a reviewer denied the action or the review expired.
        "review": (
            {key: review.get(key) for key in ("review_id", "status", "reviewer_id", "comment")}
            if isinstance(review, dict)
            else None
        ),
    }


def build_violation_response(message_id: int, cdp_session_id: str | None, violation: dict[str, Any]) -> str:
    """CDP error response for a refused command."""
    request = {"id": message_id, "sessionId": cdp_session_id}
    error = {"code": CDP_VIOLATION_ERROR_CODE, "message": encode_violation(violation)}
    return json.dumps(cdp_response(request, {"error": error}))


class ViolationReporter:
    """Per-session: records the violation, answers the agent, closes the WebSocket.

    Raw CDP clients receive the in-band error response. Playwright retries
    actions on CDP errors, so it sees the violation through the close reason
    and the /violations/<session_id> lookup (client.statelock_guard).
    """

    def __init__(
        self,
        client_ws: WebSocket,
        registry: ViolationRegistry,
        events: Events,
        close_delay: float = 0.5,
    ) -> None:
        self.client_ws = client_ws
        self.registry = registry
        self.events = events
        self.close_delay = close_delay
        self.terminating = False
        # Set with terminating: wakes an action paused for review (the session is ending).
        self.terminated = asyncio.Event()
        self.client_closed = False
        # The governor sets this to its secret scrubber: a rule's reason can quote a field value.
        self.scrub: Callable[[Any], Any] = lambda value: value
        self._violation: dict[str, Any] | None = None

    async def _record(self, context: ActionContext, verdict: PolicyVerdict) -> dict[str, Any]:
        self.terminating = True
        self.terminated.set()
        violation: dict[str, Any] = self.scrub(build_violation(context, verdict))
        self._violation = violation
        self.registry.record(violation)
        await self.events.emit(event_names.VIOLATION, violation)
        return violation

    async def reject(
        self,
        action: CdpAction,
        context: ActionContext,
        verdict: PolicyVerdict,
        close_code: int,
    ) -> None:
        """Called from the client pump: answer in-band, refuse queued commands, close."""
        violation = await self._record(context, verdict)
        try:
            if action.message_id is not None:
                await self.client_ws.send_text(
                    build_violation_response(action.message_id, action.session_id, violation)
                )
            await self._drain()
        except (WebSocketDisconnect, RuntimeError):
            logger.debug("Client closed before the violation was delivered in-band")
        await self.close(close_code, encode_close_reason(violation))

    async def terminate(
        self, context: ActionContext, verdict: PolicyVerdict, close_code: int, answer: CdpAction | None = None
    ) -> None:
        """Called outside the client pump (guard, background tasks): record, answer ``answer``
        in-band, and close without reading (only the client pump reads the agent's socket;
        it refuses what arrives meanwhile)."""
        violation = await self._record(context, verdict)
        if answer is not None and answer.message_id is not None:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                response = build_violation_response(answer.message_id, answer.session_id, violation)
                await self.client_ws.send_text(response)
        await self.close(close_code, encode_close_reason(violation))

    async def refuse(self, payload: dict[str, Any] | None) -> None:
        """Answer a command received while the session is terminating."""
        if payload is None or not isinstance(payload.get("id"), int) or self._violation is None:
            return
        response = build_violation_response(payload["id"], as_str(payload.get("sessionId")), self._violation)
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self.client_ws.send_text(response)

    async def _drain(self) -> None:
        """Refuse every command received during the close window. Nothing is forwarded.

        Playwright pipelines mouseMoved/mousePressed/mouseReleased for one click
        and waits for all three responses.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.close_delay
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                raw_message = await asyncio.wait_for(self.client_ws.receive_text(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            await self.refuse(parse_cdp_message(raw_message))

    async def close(self, code: int, reason: str) -> None:
        if self.client_closed:
            return
        self.client_closed = True
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self.client_ws.close(code=code, reason=truncate_utf8(reason))
