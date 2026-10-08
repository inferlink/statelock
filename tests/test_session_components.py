import asyncio
import json
from pathlib import Path

from helpers import context_for, mouse_action, run

from statelock.audit import LocalJsonSink, build_action_record
from statelock.audit.sequencing import SequencedWriter
from statelock.core.verdict import PolicyVerdict
from statelock.events import Events
from statelock.proxy.reporter import build_violation, build_violation_response
from statelock.proxy.targets import TargetRegistry
from statelock.wire import CDP_VIOLATION_ERROR_CODE, decode_violation


def _attached(session_id: str, target_id: str, target_type: str = "page") -> str:
    return json.dumps(
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": session_id, "targetInfo": {"targetId": target_id, "type": target_type}},
        }
    )


# TargetRegistry -------------------------------------------------------------------------------


def test_target_registry_tracks_attach_and_detach() -> None:
    targets = TargetRegistry()
    targets.track(_attached("S1", "T1"))
    targets.track('{"id": 3, "result": {}}')
    assert targets.target_for("S1") == "T1"
    targets.track(json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}}))
    assert targets.target_for("S1") is None


class _FakeSetup:
    """A setup step that only applies to pages (like the page guard)."""

    def __init__(self, *, fail: bool = False, delay: float = 0.01) -> None:
        self.fail = fail
        self.delay = delay
        self.done: list[str] = []

    def __call__(self, target_id: str, target_info: dict):
        if target_info.get("type") != "page":
            return None
        return self._run(target_id)

    async def _run(self, target_id: str) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        self.done.append(target_id)


def test_await_ready_waits_for_every_setup_step() -> None:
    async def scenario():
        first, second = _FakeSetup(), _FakeSetup(delay=0.02)
        targets = TargetRegistry()
        targets.add_setup(first)
        targets.add_setup(second)
        targets.track(_attached("S1", "T1"))
        targets.track(_attached("W1", "T2", target_type="service_worker"))
        assert await targets.await_ready("S1") is None
        assert await targets.await_ready("W1") is None
        assert first.done == ["T1"]
        assert second.done == ["T1"]
        await targets.close()

    run(scenario())


def test_await_ready_reports_setup_failure_and_timeout() -> None:
    async def scenario():
        targets = TargetRegistry(ready_timeout=0.05)
        targets.add_setup(_FakeSetup(fail=True))
        targets.track(_attached("S1", "T1"))
        failed = await targets.await_ready("S1")
        slow = TargetRegistry(ready_timeout=0.01)
        slow.add_setup(_FakeSetup(delay=1))
        slow.track(_attached("S2", "T2"))
        timed_out = await slow.await_ready("S2")
        await targets.close()
        await slow.close()
        return failed, timed_out

    assert run(scenario()) == ("boom", "target setup timed out")


def test_command_waiting_on_a_target_that_detaches_is_not_refused() -> None:
    """A tab that closes during its setup (for example a short-lived page) cancels the setup;
    a command that was waiting for it goes to the browser instead of ending the session."""

    async def scenario():
        targets = TargetRegistry()
        targets.add_setup(_FakeSetup(delay=1))
        targets.track(_attached("S1", "T1"))
        waiting = asyncio.ensure_future(targets.await_ready("S1"))
        await asyncio.sleep(0.01)
        targets.track(json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}}))
        result = await waiting
        await targets.close()
        return result

    assert run(scenario()) is None


def _detached(session_id: str) -> str:
    return json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": session_id}})


def test_target_setup_runs_once_per_target_across_attaches() -> None:
    # Attaching to a tab again must not install the page guard again (that would reset
    # what it learned, e.g. which scripts agent code created).
    async def scenario():
        setup = _FakeSetup()
        targets = TargetRegistry()
        targets.add_setup(setup)
        targets.track(_attached("S1", "T1"))
        targets.track(_attached("S2", "T1"))  # a second session while the setup runs
        assert await targets.await_ready("S1") is None
        assert await targets.await_ready("S2") is None
        targets.track(_detached("S1"))
        targets.track(_detached("S2"))
        targets.track(_attached("S3", "T1"))  # detached and attached again
        assert await targets.await_ready("S3") is None
        await targets.close()
        return setup.done

    assert run(scenario()) == ["T1"]


def test_failed_or_abandoned_target_setup_runs_again_on_attach() -> None:
    async def scenario():
        setup = _FakeSetup(fail=True)
        targets = TargetRegistry()
        targets.add_setup(setup)
        targets.track(_attached("S1", "T1"))
        failed = await targets.await_ready("S1")
        setup.fail = False
        targets.track(_attached("S2", "T1"))
        retried = await targets.await_ready("S2")
        slow = _FakeSetup(delay=1)
        other = TargetRegistry()
        other.add_setup(slow)
        other.track(_attached("S3", "T2"))
        await asyncio.sleep(0.01)
        other.track(_detached("S3"))  # cancelled: nobody waits for it
        slow.delay = 0.01
        other.track(_attached("S4", "T2"))
        restarted = await other.await_ready("S4")
        await targets.close()
        await other.close()
        return failed, retried, restarted, slow.done

    assert run(scenario()) == ("boom", None, None, ["T2"])


# SequencedWriter ------------------------------------------------------------------------------


def _record(sequence: int):
    ctx = context_for(mouse_action(), sequence=sequence, element={"text": "x"})
    return build_action_record(ctx, PolicyVerdict.allow("ok"))


def test_sequenced_writer_writes_in_order(tmp_path: Path) -> None:
    order: list[int] = []

    class RecordingSink(LocalJsonSink):
        async def write_action(self, record):
            order.append(record.sequence)
            return await super().write_action(record)

    async def scenario():
        writer = SequencedWriter(RecordingSink(tmp_path), Events())

        async def slow_first():
            async with writer.slot() as slot:
                await asyncio.sleep(0.05)
                await slot.write(_record(slot.sequence))

        async def fast_second():
            await asyncio.sleep(0.01)
            async with writer.slot() as slot:
                await slot.write(_record(slot.sequence))

        await asyncio.gather(slow_first(), fast_second())

    run(scenario())
    assert order == [1, 2]


def test_sequenced_writer_skips_unwritten_slot(tmp_path: Path) -> None:
    async def scenario():
        writer = SequencedWriter(LocalJsonSink(tmp_path), Events())
        async with writer.slot():
            pass  # never written
        async with writer.slot() as slot:
            await asyncio.wait_for(slot.write(_record(slot.sequence)), timeout=1)
        return slot.sequence

    assert run(scenario()) == 2


# Reporter ---------------------------------------------------------------------------------------


def test_build_violation_response_preserves_id_and_session() -> None:
    ctx = context_for(mouse_action(), sequence=5, agent_id="finance")
    verdict = PolicyVerdict.block(reason="mismatch", rule="assert_field_equal", policy_id="finance")
    response = json.loads(build_violation_response(42, "cdp-1", build_violation(ctx, verdict)))
    assert response["id"] == 42
    assert response["sessionId"] == "cdp-1"
    assert response["error"]["code"] == CDP_VIOLATION_ERROR_CODE
    decoded = decode_violation(response["error"]["message"])
    assert decoded["policy_id"] == "finance"
    assert decoded["violation_type"] == "pre_condition"
    assert decoded["sequence"] == 5


def test_background_tasks_log_failures_and_close(caplog) -> None:
    import logging

    from statelock.proxy.tasks import BackgroundTasks

    async def fail() -> None:
        raise RuntimeError("kaboom")

    async def forever() -> None:
        await asyncio.sleep(60)

    async def scenario():
        tasks = BackgroundTasks("unit")
        tasks.spawn(fail())
        tasks.spawn(forever())
        await asyncio.sleep(0.01)
        assert len(tasks) == 1
        await tasks.close()
        assert len(tasks) == 0

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert "unit background task failed" in caplog.text
