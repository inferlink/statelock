"""Starting and stopping the governed Chromium (fake browsers, plus one real one)."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from statelock.proxy import browser as browser_module
from statelock.proxy.browser import (
    PROFILE_LOCK,
    PROFILE_PREFIX,
    BrowserLaunchError,
    ChromiumCdpLauncher,
    GovernedBrowser,
    _Profile,
    _StderrTail,
    chromium_environment,
    sweep_stale_profiles,
)

LINUX = sys.platform.startswith("linux")


def _script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "chrome"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _failing_chromium(tmp_path: Path, message: str) -> Path:
    return _script(tmp_path, f"#!/bin/sh\necho '{message}' >&2\nexit 1\n")


def _answering_chromium(tmp_path: Path, *, ignore_term: bool = False) -> Path:
    """Answers every CDP command on the pipe (fds 3 and 4), as Chromium does; writes its pid,
    arguments and environment; starts a helper process, as Chromium does. With ignore_term it
    is a hung browser: it ignores SIGTERM and stays up after its pipe closes."""
    return _script(
        tmp_path,
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json, os, signal, subprocess, sys, time
            if {ignore_term!r}:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            helper = subprocess.Popen(["sleep", "60"], pass_fds=())
            with open({str(tmp_path / "info.json")!r}, "w") as info:
                json.dump({{"pid": os.getpid(), "helper": helper.pid, "args": sys.argv, "env": dict(os.environ)}}, info)
            data = b""
            while chunk := os.read(3, 65536):
                data += chunk
                while b"\\0" in data:
                    raw, data = data.split(b"\\0", 1)
                    message = json.loads(raw)
                    os.write(4, json.dumps({{"id": message["id"], "result": {{}}}}).encode() + b"\\0")
            while {ignore_term!r}:  # a hung browser: stays up after its pipe closes, until SIGKILL
                time.sleep(1)
            """
        ),
    )


def _silent_chromium(tmp_path: Path) -> Path:
    """Never answers and ignores SIGTERM (a hung browser)."""
    return _script(
        tmp_path,
        f"#!/bin/sh\necho $$ > {tmp_path / 'pid'}\ntrap '' TERM\nwhile true; do sleep 1; done\n",
    )


def _info(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))


def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if LINUX:  # a zombie no one has reaped yet is gone too
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _launcher(executable: Path, tmp_path: Path, **kwargs: object) -> ChromiumCdpLauncher:
    profiles = tmp_path / "profiles"
    profiles.mkdir(exist_ok=True)
    return ChromiumCdpLauncher(executable_path=executable, profile_root=profiles, **kwargs)  # type: ignore[arg-type]


def _launch_error(launcher: ChromiumCdpLauncher) -> BrowserLaunchError:
    with pytest.raises(BrowserLaunchError) as caught:
        asyncio.run(launcher.launch())
    return caught.value


# Failures ---------------------------------------------------------------------------------


def test_sandbox_failure_explains_what_to_do(tmp_path: Path) -> None:
    executable = _failing_chromium(tmp_path, "FATAL: No usable sandbox! Update your kernel")
    error = _launch_error(_launcher(executable, tmp_path, sandbox=True))
    assert "exited with code 1" in str(error)
    assert "README-sandbox.md" in str(error)
    assert "No usable sandbox" in str(error)


def test_other_launch_failures_include_stderr_without_the_sandbox_hint(tmp_path: Path) -> None:
    error = _launch_error(_launcher(_failing_chromium(tmp_path, "cannot open display"), tmp_path))
    assert "cannot open display" in str(error)
    assert "README-sandbox.md" not in str(error)
    assert not list((tmp_path / "profiles").iterdir())  # the profile is removed


def test_a_browser_that_never_answers_times_out_and_is_killed(tmp_path: Path) -> None:
    # It ignores SIGTERM: the stop ends with SIGKILL to its process group.
    launcher = _launcher(_silent_chromium(tmp_path), tmp_path, start_timeout=0.5, stop_timeout=0.5)
    started = time.monotonic()
    error = _launch_error(launcher)
    assert time.monotonic() - started < 5
    assert "did not answer on its CDP pipe within 0.5 s" in str(error)
    assert _wait_dead(int((tmp_path / "pid").read_text()))
    assert not list((tmp_path / "profiles").iterdir())


def test_a_cancelled_launch_kills_the_browser(tmp_path: Path) -> None:
    launcher = _launcher(_silent_chromium(tmp_path), tmp_path, start_timeout=30, stop_timeout=0.5)

    async def scenario() -> None:
        task = asyncio.create_task(launcher.launch())
        await asyncio.to_thread(_wait_for_file, tmp_path / "pid")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _wait_dead(int((tmp_path / "pid").read_text()))
    assert not list((tmp_path / "profiles").iterdir())


# A running browser ------------------------------------------------------------------------


def test_launch_uses_the_cdp_pipe_and_a_minimal_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATELOCK_PERCEPTION_API_KEY", "sk-not-for-chromium")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")

    async def scenario() -> dict:
        browser = await _launcher(_answering_chromium(tmp_path), tmp_path).launch()
        try:
            assert await browser.connection.send("Browser.getVersion") == {}
            info = _info(tmp_path)
            assert os.getsid(info["pid"]) == info["pid"]  # its own session and process group
            return info
        finally:
            await browser.close()

    info = asyncio.run(scenario())
    assert "--remote-debugging-pipe" in info["args"]
    assert not any(arg.startswith("--remote-debugging-port") for arg in info["args"])
    assert "STATELOCK_PERCEPTION_API_KEY" not in info["env"]
    assert info["env"].get("LC_ALL") == "C.UTF-8" and "PATH" in info["env"]
    assert _wait_dead(info["pid"]) and _wait_dead(info["helper"])  # the whole process group
    assert not list((tmp_path / "profiles").iterdir())


def test_close_kills_a_browser_that_ignores_sigterm(tmp_path: Path) -> None:
    async def scenario() -> float:
        browser = await _launcher(_answering_chromium(tmp_path, ignore_term=True), tmp_path, stop_timeout=0.5).launch()
        started = time.monotonic()
        await browser.close()
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5
    info = _info(tmp_path)
    assert _wait_dead(info["pid"]) and _wait_dead(info["helper"])


def test_close_stops_the_browser_even_when_a_step_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        browser = await _launcher(_answering_chromium(tmp_path), tmp_path).launch()

        async def broken() -> None:
            raise RuntimeError("connection close failed")

        browser.connection.close = broken  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await browser.close()

    asyncio.run(scenario())
    assert _wait_dead(_info(tmp_path)["pid"])
    assert not list((tmp_path / "profiles").iterdir())


def test_a_cancelled_close_still_kills_the_browser(tmp_path: Path) -> None:
    async def scenario() -> None:
        browser = await _launcher(_answering_chromium(tmp_path, ignore_term=True), tmp_path, stop_timeout=30).launch()
        task = asyncio.create_task(browser.close())
        await asyncio.sleep(0.3)  # waiting for the SIGTERM it ignores
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _wait_dead(_info(tmp_path)["pid"])


@pytest.mark.skipif(not LINUX, reason="PR_SET_PDEATHSIG is Linux only")
def test_the_browser_dies_with_the_proxy(tmp_path: Path) -> None:
    executable = _answering_chromium(tmp_path, ignore_term=True)
    proxy = textwrap.dedent(
        f"""
        import asyncio, os, sys
        from pathlib import Path
        from statelock.proxy.browser import ChromiumCdpLauncher

        async def main():
            launcher = ChromiumCdpLauncher(executable_path=Path({str(executable)!r}), profile_root=Path({str(tmp_path)!r}))
            await launcher.launch()
            os.kill(os.getpid(), 9)  # the proxy is killed: no cleanup runs

        asyncio.run(main())
        """
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(browser_module.__file__).parents[2])}
    subprocess.run([sys.executable, "-c", proxy], env=environment, timeout=60, check=False)
    assert _wait_dead(_info(tmp_path)["pid"])


# Profile folders and stderr ---------------------------------------------------------------


def test_startup_sweep_removes_only_profiles_no_proxy_holds(tmp_path: Path) -> None:
    stale = tmp_path / f"{PROFILE_PREFIX}stale"
    stale.mkdir()
    (stale / PROFILE_LOCK).touch()
    young_unlocked = tmp_path / f"{PROFILE_PREFIX}starting"
    young_unlocked.mkdir()
    old_unlocked = tmp_path / f"{PROFILE_PREFIX}old"
    old_unlocked.mkdir()
    os.utime(old_unlocked, (0, 0))
    other = tmp_path / "unrelated"
    other.mkdir()
    live = _Profile(tmp_path)
    try:
        assert sorted(sweep_stale_profiles(tmp_path)) == sorted([stale, old_unlocked])
        assert live.path.exists() and young_unlocked.exists() and other.exists()
    finally:
        asyncio.run(live.remove())
    assert not live.path.exists()


def test_the_launcher_sweeps_its_own_profile_root(tmp_path: Path) -> None:
    stale = tmp_path / f"{PROFILE_PREFIX}stale"
    stale.mkdir()
    (stale / PROFILE_LOCK).touch()
    assert ChromiumCdpLauncher(profile_root=tmp_path).sweep_stale_profiles() == [stale]


def test_stderr_keeps_only_its_tail() -> None:
    async def scenario() -> _StderrTail:
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 1_000_000 + b"the last words")
        stream.feed_eof()
        tail = _StderrTail()
        await tail.drain(stream)
        return tail

    tail = asyncio.run(scenario())
    assert len(tail.data) == browser_module.STDERR_TAIL_BYTES
    assert tail.text().endswith("the last words")


def test_chromium_environment_keeps_only_what_chromium_needs() -> None:
    environment = chromium_environment(
        {"HOME": "/h", "PATH": "/bin", "XDG_RUNTIME_DIR": "/run", "STATELOCK_API_KEY": "k", "AWS_SECRET": "s"}
    )
    assert environment == {"HOME": "/h", "PATH": "/bin", "XDG_RUNTIME_DIR": "/run"}


# Real Chromium ------------------------------------------------------------------------------


def _socket_inodes(pid: int) -> set[str]:
    inodes = set()
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    return inodes


def _listening_inodes() -> set[str]:
    inodes = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.exists():  # no IPv6 on this host
            continue
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":  # LISTEN
                inodes.add(fields[9])
    return inodes


@pytest.mark.browser
@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_real_chromium_listens_on_no_tcp_port(tmp_path: Path) -> None:
    browser_support = pytest.importorskip("browser_support")
    if not browser_support.chromium_available():
        pytest.skip("Playwright Chromium is not installed")

    async def scenario() -> tuple[set[str], list[int], str]:
        browser: GovernedBrowser = await ChromiumCdpLauncher(profile_root=tmp_path).launch()
        try:
            group = browser.process.pid
            await browser.connection.send("Target.getTargets")
            await asyncio.sleep(1.0)  # let every helper process start
            pids = [
                int(entry.name)
                for entry in Path("/proc").iterdir()
                if entry.name.isdigit() and _pgid(int(entry.name)) == group
            ]
            sockets = set().union(*(_socket_inodes(pid) for pid in pids))
            cmdline = Path(f"/proc/{group}/cmdline").read_text().replace("\0", " ")
            return sockets & _listening_inodes(), pids, cmdline
        finally:
            await browser.close()

    listening, pids, cmdline = asyncio.run(scenario())
    assert len(pids) > 1  # the browser and its helpers were inspected
    assert listening == set()
    assert "--remote-debugging-pipe" in cmdline and "--remote-debugging-port" not in cmdline


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def test_cdp_command_timeout_setting_reaches_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    from statelock.services import launcher
    from statelock.settings import Settings

    monkeypatch.setenv("STATELOCK_CDP_COMMAND_TIMEOUT", "7.5")
    assert launcher(Settings()).command_timeout == 7.5
