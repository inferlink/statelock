"""Shared test helpers (imported by test modules; fixtures live in conftest.py)."""

from __future__ import annotations

import asyncio
from typing import Any

from statelock.core.actions import ActionKind, CdpAction
from statelock.core.state import ActionContext, BrowserState, TargetElement

URL = "http://proxy.test/demo/finance?scenario=match"


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def mouse_action(event_type: str = "mousePressed", session_id: str | None = "S1") -> CdpAction:
    return CdpAction(
        message_id=1,
        method="Input.dispatchMouseEvent",
        kind=ActionKind.MOUSE,
        params={"type": event_type, "x": 10, "y": 20},
        session_id=session_id,
    )


def key_action(event_type: str = "keyDown", key: str = "Enter", code: str = "Enter") -> CdpAction:
    return CdpAction(
        message_id=2,
        method="Input.dispatchKeyEvent",
        kind=ActionKind.KEYBOARD,
        params={"type": event_type, "key": key, "code": code},
        session_id="S1",
    )


def context_for(
    action: CdpAction,
    *,
    agent_id: str = "agent",
    url: str = URL,
    element: dict[str, Any] | None = None,
    state: BrowserState | None = None,
    sequence: int = 1,
    session_id: str = "11111111-2222-3333-4444-555555555555",
    **state_fields: Any,
) -> ActionContext:
    if state is None:
        state = BrowserState(
            url=url,
            target_element=TargetElement.model_validate(element) if element is not None else None,
            **state_fields,
        )
    return ActionContext.from_action(
        session_id=session_id, sequence=sequence, action=action, browser_state=state, agent_id=agent_id
    )
