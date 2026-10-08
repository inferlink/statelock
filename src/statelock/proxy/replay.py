# SPDX-License-Identifier: Apache-2.0
"""Script clicks replayed as governed input.

A plain ``element.click()`` from the agent's page code is cancelled by the page
guard and handed here: Statelock replays it as real mouse input at the element,
and each replayed event is captured, checked and recorded like the agent's own
clicks (see ActionGovernor).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from statelock.audit.records import build_action_record
from statelock.core.actions import MOUSE_METHOD, ActionKind, CdpAction
from statelock.core.verdict import PolicyVerdict
from statelock.proxy.connection import CdpError
from statelock.proxy.governor import ActionGovernor
from statelock.proxy.guard import ReplayTarget
from statelock.proxy.pages import PageSessions
from statelock.proxy.scripts import AgentScripts
from statelock.wire import CLOSE_CODE_POST_CONDITION, CLOSE_CODE_PRE_CONDITION

logger = logging.getLogger(__name__)

# A replayed script click: (type, button, buttons, clickCount) of each mouse event.
REPLAY_EVENTS = (
    ("mouseMoved", "none", 0, 0),
    ("mousePressed", "left", 1, 1),
    ("mouseReleased", "left", 0, 1),
)
# Times one replayed event is located again when the element moved before it was sent.
REPLAY_ATTEMPTS = 3


class _Replayed(Enum):
    SENT = "sent"
    MOVED = "moved"  # the element was no longer at the point; nothing was sent
    ENDED = "ended"  # blocked: the session ends


class ScriptClickReplayer:
    """Replays the agent's script clicks (the page guard's ``on_script_click``) through
    the session's governor, on Statelock's own CDP session of the tab.

    An element a real click would not hit (covered, hidden, gone), or a replay that
    cannot complete, is a guard violation: the session ends.
    """

    def __init__(self, governor: ActionGovernor, pages: PageSessions, scripts: AgentScripts) -> None:
        self.governor = governor
        self.pages = pages
        self.scripts = scripts
        # Script clicks being replayed; the agent's next command waits for them.
        self._replays = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def replay(self, target_id: str, event: dict[str, Any], target: ReplayTarget) -> None:
        """Replay a click the agent's page code made (element.click()) as real mouse
        input at the element, governed and recorded like the agent's own clicks."""
        governor = self.governor
        if governor.reporter.terminating:
            return
        self._replays += 1
        self._idle.clear()
        try:
            point = await self._locate(target)
            if point is None:
                await governor.on_guard_violation(target_id, event)
            else:
                await self._replay_click(target_id, event, target, point)
        except Exception:
            # The click was cancelled in the page, so nothing was clicked; the agent learns why.
            logger.exception("Replaying a script click failed in session %s", governor.session_id)
            await governor.on_guard_violation(target_id, event)
        finally:
            await target.release()
            self._replays -= 1
            if self._replays == 0:
                self._idle.set()

    async def done(self, timeout: float) -> None:
        """Wait (bounded) until replayed script clicks finished, so the agent's next
        command sees their effect."""
        if self._replays:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout=timeout)

    async def _locate(self, target: ReplayTarget) -> dict[str, float] | None:
        try:
            return await target.locate()
        except CdpError as error:
            logger.warning(
                "Could not locate a script-clicked element in session %s: %s", self.governor.session_id, error
            )
            return None

    async def _replay_click(
        self, target_id: str, event: dict[str, Any], target: ReplayTarget, point: dict[str, float]
    ) -> None:
        """Move, press and release at the element. Each event is captured and checked at its
        point, and sent only if the element is still there (the layout can move between
        events, e.g. a viewport resize); otherwise the element is located again."""
        session_id = await self.pages.session_for(target_id)
        replayed = {key: event.get(key) for key in ("event_type", "target", "url")}
        for event_type, button, buttons, count in REPLAY_EVENTS:
            outcome = _Replayed.MOVED
            for _attempt in range(REPLAY_ATTEMPTS):
                params = {"type": event_type, **point, "button": button, "buttons": buttons, "clickCount": count}
                action = CdpAction(
                    message_id=None,
                    method=MOUSE_METHOD,
                    kind=ActionKind.MOUSE,
                    params={**params, "statelock_replayed_script_click": replayed},
                )

                async def send(params: dict[str, Any] = params) -> None:
                    self.scripts.input_dispatched(target_id)
                    await self.pages.connection.send(MOUSE_METHOD, params, session_id=session_id)

                outcome = await self._govern_replayed(action, target_id, send, target)
                if outcome is not _Replayed.MOVED:
                    break
                located = await self._locate(target)
                if located is None:
                    break
                point = located
            if outcome is _Replayed.ENDED:
                return
            if outcome is _Replayed.MOVED:
                # Nothing was clicked, or the target moved after the press: ending the session
                # is the only safe outcome (releasing at the old point could click another element).
                await self.governor.on_guard_violation(target_id, event)
                return

    async def _govern_replayed(
        self,
        action: CdpAction,
        target_id: str,
        send: Callable[[], Awaitable[None]],
        target: ReplayTarget | None,
    ) -> _Replayed:
        """Govern one replayed mouse event. With ``target``, it is sent only if the element
        is still at the event's point when the checks are done."""
        governor = self.governor
        point = {"x": action.params["x"], "y": action.params["y"]}
        async with governor.writer.slot() as slot:
            context, verdict, review = await governor.decide_input(slot, action, target_id)
            if governor.reporter.terminating:
                await slot.write(build_action_record(context, verdict, review=review))
                return _Replayed.ENDED
            if verdict.blocked:
                await governor.reporter.terminate(context, verdict, CLOSE_CODE_PRE_CONDITION)
                await slot.write(build_action_record(context, verdict, review=review))
                return _Replayed.ENDED
            if target is not None and not await self._still_hits(target, point):
                context.params = {**context.params, "statelock_replay_not_sent": "element_moved"}
                moved = PolicyVerdict.allow(
                    "Not sent: the element moved before the replayed event; Statelock locates it again",
                    verdict.evidence,
                )
                await slot.write(build_action_record(context, moved, review=review))
                return _Replayed.MOVED
            await send()
            post_verdict = await governor.post_check(slot, action, context, target_id) if action.is_commit else None
            if post_verdict is not None and post_verdict.blocked:
                await governor.reporter.terminate(context, post_verdict, CLOSE_CODE_POST_CONDITION)
            await slot.write(build_action_record(context, verdict, post_verdict, review=review))
        logger.info(
            "Replayed script click %s session=%s verdict=%s",
            action.params.get("type"),
            governor.session_id,
            verdict.decision.value,
        )
        return _Replayed.ENDED if post_verdict is not None and post_verdict.blocked else _Replayed.SENT

    async def _still_hits(self, target: ReplayTarget, point: dict[str, float]) -> bool:
        try:
            return await target.hits(point)
        except CdpError:
            return False
