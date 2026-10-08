"""A script click Statelock cannot replay safely ends the session (ScriptClickReplayer)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from helpers import context_for, run

from statelock.core.actions import CdpAction
from statelock.core.verdict import PolicyVerdict
from statelock.proxy.replay import ScriptClickReplayer
from statelock.proxy.scripts import AgentScripts

CLICK = {"kind": "synthetic_event", "event_type": "click", "replay_id": 3}


class _Slot:
    sequence = 1

    async def write(self, _record: Any) -> str:
        return "record"


class _Writer:
    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[_Slot]:
        yield _Slot()


class _Governor:
    """The governor steps a replay uses; every event is allowed unless decide_input is replaced."""

    session_id = "session"

    def __init__(self) -> None:
        self.reporter = SimpleNamespace(terminating=False)
        self.writer = _Writer()
        self.violations: list[tuple[str, dict[str, Any]]] = []

    async def decide_input(self, _slot: Any, action: CdpAction, _target_id: str) -> tuple[Any, PolicyVerdict, None]:
        return context_for(action), PolicyVerdict.allow("ok", {}), None

    async def post_check(self, *_args: Any) -> None:
        return None

    async def on_guard_violation(self, target_id: str, event: dict[str, Any]) -> None:
        self.violations.append((target_id, event))


class _Pages:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.connection = self

    async def session_for(self, _target_id: str) -> str:
        return "G1"

    async def send(self, _method: str, params: dict[str, Any], session_id: str) -> dict[str, Any]:  # noqa: ARG002
        self.sent.append(params)
        return {}


class _Target:
    """The clicked element: found at (5, 5) each time; ``hits`` says whether it is still there."""

    def __init__(self, hits: list[bool]) -> None:
        self._hits = hits
        self.released = False

    async def locate(self) -> dict[str, float]:
        return {"x": 5.0, "y": 5.0}

    async def hits(self, _point: dict[str, float]) -> bool:
        return self._hits.pop(0) if self._hits else False

    async def release(self) -> None:
        self.released = True


def _replay(governor: _Governor, target: _Target) -> _Pages:
    pages = _Pages()
    replayer = ScriptClickReplayer(governor, pages, AgentScripts())  # type: ignore[arg-type]

    async def scenario() -> None:
        await replayer.replay("T1", CLICK, target)  # type: ignore[arg-type]
        await replayer.done(1.0)

    run(scenario())
    return pages


def test_a_replay_that_fails_is_reported_as_a_violation() -> None:
    governor = _Governor()

    async def broken(*_args: Any) -> None:
        raise RuntimeError("capture crashed")

    governor.decide_input = broken  # type: ignore[method-assign]
    target = _Target([True, True, True])
    pages = _replay(governor, target)
    assert governor.violations == [("T1", CLICK)]
    assert pages.sent == [] and target.released


def test_an_element_that_moves_after_the_press_ends_the_session_without_a_release() -> None:
    governor = _Governor()
    target = _Target([True, True])  # there for the move and the press, then gone
    pages = _replay(governor, target)
    assert [event["type"] for event in pages.sent] == ["mouseMoved", "mousePressed"]
    assert governor.violations == [("T1", CLICK)]


def test_a_replay_where_the_element_stays_put_sends_a_full_click() -> None:
    governor = _Governor()
    pages = _replay(governor, _Target([True, True, True]))
    assert [event["type"] for event in pages.sent] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert governor.violations == []
