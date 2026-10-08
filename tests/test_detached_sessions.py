"""Per-session state is dropped when one of Statelock's CDP sessions detaches (PageSessions.on_detached)."""

from __future__ import annotations

from fakes import FakeConnection
from helpers import run

from statelock.proxy.connection import DETACHED_EVENT
from statelock.proxy.guard import PageGuard, TargetGuardState
from statelock.proxy.inspector import Inspector
from statelock.proxy.pages import PageSessions
from statelock.proxy.worlds import STATE_WORLD, IsolatedWorld


def _detach(connection: FakeConnection, session_id: str) -> None:
    connection.emit({"method": DETACHED_EVENT, "params": {"sessionId": session_id}})


def test_detach_callbacks_run_for_each_detached_session_even_after_one_fails() -> None:
    connection = FakeConnection()
    pages = PageSessions(connection)
    seen: list[str] = []

    def broken(_session_id: str) -> None:
        raise RuntimeError("cleanup failed")

    pages.on_detached(broken)
    pages.on_detached(seen.append)
    session_id = run(pages.session_for("T1"))
    _detach(connection, session_id)
    _detach(connection, "S-unknown")  # a session PageSessions never attached
    connection.emit({"method": "Target.attachedToTarget", "params": {"sessionId": "S2"}})
    assert seen == [session_id, "S-unknown"]
    assert pages.target_for_session(session_id) is None


def test_guard_world_and_inspector_drop_a_detached_sessions_state() -> None:
    async def noop(*_args: object) -> None:
        return None

    connection = FakeConnection()
    pages = PageSessions(connection)
    guard = PageGuard(pages, None, noop)
    world = IsolatedWorld(pages, STATE_WORLD)
    inspector = Inspector(pages)
    guard._states["G1"] = TargetGuardState(target_id="T1", session_id="G1")
    guard._states["G2"] = TargetGuardState(target_id="T2", session_id="G2")
    world._contexts["G1"] = {"T1": 4}
    inspector._touch_starts["G1"] = {"x": 1.0, "y": 2.0}
    _detach(connection, "G1")
    assert set(guard._states) == {"G2"}
    assert "G1" not in world._contexts
    assert "G1" not in inspector._touch_starts
