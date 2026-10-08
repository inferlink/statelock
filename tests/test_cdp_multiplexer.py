"""Statelock's and the agent's CDP clients sharing the browser's one pipe."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from statelock.proxy.browser import ChromiumCdpLauncher
from statelock.proxy.connection import BrowserClosedError, CdpConnection, CdpError, CdpMultiplexer


class FakePipe:
    """The browser's side of the pipe: records what was written, emits what a test feeds."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def read(self) -> str | None:
        return await self._incoming.get()

    def write(self, message: str) -> None:
        self.written.append(json.loads(message))

    def close(self) -> None:
        self._incoming.put_nowait(None)

    def emit(self, message: dict[str, Any]) -> None:
        self._incoming.put_nowait(json.dumps(message))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _setup() -> tuple[FakePipe, CdpMultiplexer]:
    pipe = FakePipe()
    multiplexer = CdpMultiplexer(pipe)  # type: ignore[arg-type]
    multiplexer.start()
    return pipe, multiplexer


async def _recv(channel: Any) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(channel.recv(), timeout=1))


async def _nothing(channel: Any) -> bool:
    try:
        await asyncio.wait_for(channel.recv(), timeout=0.05)
    except asyncio.TimeoutError:
        return True
    return False


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _attached(session_id: str, target_id: str, parent: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "method": "Target.attachedToTarget",
        "params": {"sessionId": session_id, "targetInfo": {"targetId": target_id, "type": "page"}},
    }
    if parent is not None:
        event["sessionId"] = parent
    return event


def test_command_ids_are_renumbered_and_restored() -> None:
    async def scenario() -> None:
        pipe, mux = await _setup()
        mux.statelock.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        mux.agent.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        first, second = pipe.written
        assert first["id"] != second["id"]
        pipe.emit({"id": second["id"], "result": {"targetInfos": []}})
        pipe.emit({"id": first["id"], "result": {"product": "Chrome"}})
        assert await _recv(mux.agent) == {"id": 1, "result": {"targetInfos": []}}
        assert await _recv(mux.statelock) == {"id": 1, "result": {"product": "Chrome"}}

    run(scenario())


def test_a_session_belongs_to_the_channel_that_attached_it() -> None:
    async def scenario() -> None:
        pipe, mux = await _setup()
        mux.statelock.send(json.dumps({"id": 7, "method": "Target.attachToTarget", "params": {"targetId": "T"}}))
        # Chromium sends the attach event before the answer.
        pipe.emit(_attached("S-statelock", "T"))
        await _settle()
        assert await _nothing(mux.agent)
        pipe.emit({"id": pipe.written[0]["id"], "result": {"sessionId": "S-statelock"}})
        assert (await _recv(mux.statelock))["method"] == "Target.attachedToTarget"
        assert await _recv(mux.statelock) == {"id": 7, "result": {"sessionId": "S-statelock"}}

        # The agent cannot use or detach Statelock's session.
        mux.agent.send(json.dumps({"id": 1, "sessionId": "S-statelock", "method": "Runtime.evaluate"}))
        answer = await _recv(mux.agent)
        assert answer["id"] == 1 and answer["sessionId"] == "S-statelock"
        assert answer["error"]["message"] == "Session with given id not found."
        for params in ({"sessionId": "S-statelock"}, {"targetId": "T"}):
            mux.agent.send(json.dumps({"id": 2, "method": "Target.detachFromTarget", "params": params}))
            assert (await _recv(mux.agent))["error"]["message"] == "No session with given id"
        assert len(pipe.written) == 1  # none of these reached the browser

        # Events of Statelock's session reach only Statelock.
        pipe.emit({"sessionId": "S-statelock", "method": "Runtime.consoleAPICalled", "params": {}})
        assert (await _recv(mux.statelock))["method"] == "Runtime.consoleAPICalled"
        assert await _nothing(mux.agent)

        # Statelock's own commands on it go through.
        mux.statelock.send(json.dumps({"id": 8, "sessionId": "S-statelock", "method": "Runtime.enable"}))
        assert pipe.written[-1]["sessionId"] == "S-statelock"

    run(scenario())


def test_the_agents_auto_attached_sessions_are_the_agents() -> None:
    async def scenario() -> None:
        pipe, mux = await _setup()
        # Statelock attaches to T while the agent's auto-attach reports its own session to T.
        mux.statelock.send(json.dumps({"id": 1, "method": "Target.attachToTarget", "params": {"targetId": "T"}}))
        pipe.emit(_attached("S-agent", "T"))
        pipe.emit(_attached("S-statelock", "T"))
        pipe.emit({"id": pipe.written[0]["id"], "result": {"sessionId": "S-statelock"}})
        assert (await _recv(mux.statelock))["params"]["sessionId"] == "S-statelock"
        assert (await _recv(mux.statelock))["result"] == {"sessionId": "S-statelock"}
        assert (await _recv(mux.agent))["params"]["sessionId"] == "S-agent"
        assert await _nothing(mux.statelock)

        # A child target auto-attached under the agent's session is the agent's too.
        pipe.emit(_attached("S-child", "IFRAME", parent="S-agent"))
        assert (await _recv(mux.agent))["params"]["sessionId"] == "S-child"
        mux.agent.send(json.dumps({"id": 5, "sessionId": "S-child", "method": "Runtime.enable"}))
        assert pipe.written[-1]["sessionId"] == "S-child"
        mux.statelock.send(json.dumps({"id": 6, "sessionId": "S-agent", "method": "Runtime.enable"}))
        assert "error" in await _recv(mux.statelock)

        # Detached: the session is nobody's any more.
        pipe.emit({"method": "Target.detachedFromTarget", "params": {"sessionId": "S-agent", "targetId": "T"}})
        assert (await _recv(mux.agent))["method"] == "Target.detachedFromTarget"
        mux.agent.send(json.dumps({"id": 9, "sessionId": "S-agent", "method": "Runtime.enable"}))
        assert "error" in await _recv(mux.agent)

    run(scenario())


def test_browser_events_go_to_statelock_and_target_events_to_the_agent() -> None:
    async def scenario() -> None:
        pipe, mux = await _setup()
        pipe.emit({"method": "Browser.downloadWillBegin", "params": {"guid": "g"}})
        pipe.emit({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "T"}}})
        assert (await _recv(mux.statelock))["method"] == "Browser.downloadWillBegin"
        assert (await _recv(mux.agent))["method"] == "Target.targetCreated"
        assert await _nothing(mux.statelock) and await _nothing(mux.agent)

    run(scenario())


def test_a_closed_pipe_ends_both_channels() -> None:
    async def scenario() -> None:
        pipe, mux = await _setup()
        connection = CdpConnection(mux.statelock, command_timeout=30)
        connection.open()
        pending = asyncio.create_task(connection.send("Browser.getVersion"))
        await _settle()
        pipe.close()
        with pytest.raises(CdpError):
            await asyncio.wait_for(pending, timeout=1)
        with pytest.raises(BrowserClosedError):
            await mux.agent.recv()
        with pytest.raises(BrowserClosedError):
            mux.agent.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        with pytest.raises(CdpError):
            await connection.send("Browser.getVersion")

    run(scenario())


@pytest.mark.browser
def test_real_chromium_keeps_the_two_clients_apart(tmp_path: Path) -> None:
    browser_support = pytest.importorskip("browser_support")
    if not browser_support.chromium_available():
        pytest.skip("Playwright Chromium is not installed")

    async def scenario() -> None:
        browser = await ChromiumCdpLauncher(profile_root=tmp_path).launch()
        try:
            connection, agent = browser.connection, browser.multiplexer.agent
            targets = (await connection.send("Target.getTargets"))["targetInfos"]
            target_id = next(t["targetId"] for t in targets if t["type"] == "page")
            ours = (await connection.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))[
                "sessionId"
            ]
            agent.send(
                json.dumps(
                    {"id": 1, "method": "Target.attachToTarget", "params": {"targetId": target_id, "flatten": True}}
                )
            )
            theirs = None
            while theirs is None:
                message = await _recv(agent)
                if message.get("id") == 1:
                    theirs = message["result"]["sessionId"]
            assert theirs != ours
            expression = {"expression": "1 + 1", "returnByValue": True}
            agent.send(json.dumps({"id": 2, "sessionId": ours, "method": "Runtime.evaluate", "params": expression}))
            agent.send(json.dumps({"id": 3, "sessionId": theirs, "method": "Runtime.evaluate", "params": expression}))
            answers = {}
            while len(answers) < 2:
                message = await _recv(agent)
                if message.get("id") in (2, 3):
                    answers[message["id"]] = message
            assert answers[2]["error"]["message"] == "Session with given id not found."
            assert answers[3]["result"]["result"]["value"] == 2
            evaluated = await connection.send("Runtime.evaluate", expression, session_id=ours)
            assert evaluated["result"]["value"] == 2
        finally:
            await browser.close()

    run(scenario())
