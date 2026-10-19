"""A launch that fails while opening the CDP pipe leaves no pipe open."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_browser_launch import _answering_chromium, _launcher

from statelock.proxy.browser import ChromiumCdpLauncher

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="counts fds in /proc")


def _open_pipes() -> set[str]:
    pipes = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except FileNotFoundError:  # the listing's own fd
            continue
        if target.startswith("pipe:"):
            pipes.add(target)
    return pipes


@pytest.mark.parametrize(
    ("failing", "call"),
    [("connect_read_pipe", 1), ("connect_read_pipe", 2), ("connect_write_pipe", 1)],
)
def test_a_launch_that_fails_while_opening_a_pipe_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str, call: int
) -> None:
    launcher: ChromiumCdpLauncher = _launcher(_answering_chromium(tmp_path), tmp_path)
    original = getattr(asyncio.BaseEventLoop, failing)
    calls = {"n": 0}

    async def fail_one_call(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Two read pipes are opened (the CDP pipe and Chromium's stderr): fail one of them.
        calls["n"] += 1
        if calls["n"] != call:
            return await original(self, *args, **kwargs)
        raise OSError("no pipe for you")

    async def scenario() -> None:
        monkeypatch.setattr(asyncio.BaseEventLoop, failing, fail_one_call)
        try:
            browser = await launcher.launch()
        except OSError as error:
            assert "no pipe" in str(error)
        else:  # the stderr pipe failed: the browser still runs, without its stderr
            await browser.close()
        monkeypatch.undo()
        await asyncio.sleep(0.2)  # the stderr reader's transport closes on the next loop turns

    before = _open_pipes()
    asyncio.run(scenario())
    assert _open_pipes() <= before
