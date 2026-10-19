"""Chromium gets its CDP pipe on fds 3 and 4 whatever event loop runs the proxy.

uvicorn runs on uvloop when it is installed, and uvloop on macOS spawns with
posix_spawn, which never runs the child setup that moves the pipe to fds 3 and 4.
"""

import asyncio
from pathlib import Path
from typing import Any, NoReturn

import pytest
from test_browser_launch import _answering_chromium, _launcher

uvloop = pytest.importorskip("uvloop")


async def _launch_and_close(executable: Path, tmp_path: Path) -> None:
    browser = await _launcher(executable, tmp_path).launch()
    await browser.close()


def test_launch_under_uvloop(tmp_path: Path) -> None:
    uvloop.run(_launch_and_close(_answering_chromium(tmp_path), tmp_path))


def test_launch_does_not_use_the_event_loops_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise AssertionError("the event loop's spawn may skip preexec_fn (uvloop on macOS)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(asyncio.BaseEventLoop, "subprocess_exec", refuse)
    asyncio.run(_launch_and_close(_answering_chromium(tmp_path), tmp_path))
