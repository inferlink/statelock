# SPDX-License-Identifier: Apache-2.0
"""Launch the governed Chromium, with CDP over a pipe, and stop it again.

CDP runs over ``--remote-debugging-pipe`` (see ``proxy.connection``): Chromium
listens on no TCP port, so pages in it cannot reach its DevTools endpoints.

Chromium is tied to the proxy: it runs in its own process group, which is
stopped as a whole (SIGTERM, then SIGKILL after a timeout, also when the stop is
cancelled), and on Linux it is killed when the proxy dies. Each profile folder
holds a lock while its proxy runs, so a later proxy can remove the folders that
a crashed one left behind (``sweep_stale_profiles``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import async_playwright

from statelock.proxy.connection import DEFAULT_COMMAND_TIMEOUT, CdpConnection, CdpError, CdpMultiplexer, CdpPipe

logger = logging.getLogger(__name__)

DEFAULT_START_TIMEOUT = 10.0
STOP_TIMEOUT = 5.0
EXIT_CODE_WAIT = 1.0  # after the pipe closed during startup: for the exit code and the last stderr
CLEANUP_ATTEMPTS = 10
CLEANUP_INTERVAL = 0.2
STDERR_TAIL_BYTES = 2000
PROFILE_PREFIX = "statelock-chromium-"
PROFILE_LOCK = "statelock.lock"
# A profile folder without a lock is being created, or was left by a crash right then.
UNLOCKED_PROFILE_AGE = 3600.0
# Chromium's CDP pipe: it reads commands from fd 3 and writes answers and events to fd 4.
CHROMIUM_READ_FD = 3
CHROMIUM_WRITE_FD = 4
FIRST_FREE_FD = 10  # where the child's pipe ends wait before they are moved to 3 and 4
PR_SET_PDEATHSIG = 1
# What Chromium needs from the environment. Nothing else is passed: the proxy's
# environment holds secrets (API keys, secret values) that the browser has no use for.
CHROMIUM_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "LD_LIBRARY_PATH",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        # The host's outbound proxy, which Chromium honours.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    }
)
CHROMIUM_ENV_PREFIXES = ("LC_", "XDG_")
SANDBOX_HINT = (
    "Chromium could not start its sandbox. Run Statelock as a non-root user with a seccomp profile "
    "that allows user namespaces (compose.yaml does; see docker/README-sandbox.md), or set "
    "STATELOCK_CHROMIUM_SANDBOX=0."
)


class BrowserLaunchError(RuntimeError):
    pass


def chromium_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment Chromium is started with: only what it needs from the proxy's."""
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name in CHROMIUM_ENV_NAMES or name.startswith(CHROMIUM_ENV_PREFIXES)
    }


# Process lifetime ------------------------------------------------------------------------


def _signal_group(process: asyncio.subprocess.Process, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signum)  # the process leads its own group (start_new_session)


async def stop_process(process: asyncio.subprocess.Process, timeout: float = STOP_TIMEOUT) -> None:
    """Stop Chromium and its helpers: SIGTERM to its process group, then SIGKILL to the
    group after ``timeout`` seconds, once it has exited, or when this is cancelled."""
    try:
        if process.returncode is None:
            _signal_group(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Chromium did not stop within %.1f s; killing its process group", timeout)
    finally:
        _signal_group(process, signal.SIGKILL)  # also any helper that outlived the main process
        if process.returncode is None:
            await asyncio.shield(process.wait())


def _child_setup(read_fd: int, write_fd: int) -> Callable[[], None]:
    """Runs in the child before exec: the pipe ends to fds 3 and 4, and on Linux a SIGKILL
    when the proxy dies. Everything it calls is resolved before the fork."""
    dup2 = os.dup2
    parent = os.getpid()
    getppid = os.getppid
    exit_now = os._exit
    prctl = ctypes.CDLL(None, use_errno=True).prctl if sys.platform.startswith("linux") else None

    def setup() -> None:
        dup2(read_fd, CHROMIUM_READ_FD)
        dup2(write_fd, CHROMIUM_WRITE_FD)
        if prctl is not None:
            prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
            if getppid() != parent:  # the proxy died before the signal was set up
                exit_now(1)

    return setup


@dataclass
class _StderrTail:
    """The last bytes Chromium wrote to stderr, read as it writes (never a growing file)."""

    data: bytearray = field(default_factory=bytearray)

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            self.data.extend(chunk)
            del self.data[:-STDERR_TAIL_BYTES]

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace").strip()


# Profile folders --------------------------------------------------------------------------


class _Profile:
    """Chromium's user-data folder, locked while this proxy uses it."""

    def __init__(self, root: Path) -> None:
        self.path = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir=root))
        self._lock_fd: int | None = os.open(self.path / PROFILE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)

    async def remove(self) -> None:
        """Remove the folder, then release the lock. Chromium's helper processes can still
        write into the profile for a moment after the main process exits, so retry."""
        try:
            for attempt in range(CLEANUP_ATTEMPTS):
                try:
                    await asyncio.to_thread(shutil.rmtree, self.path)
                    return
                except FileNotFoundError:
                    return
                except OSError as error:
                    if attempt == CLEANUP_ATTEMPTS - 1:
                        logger.warning("Could not fully remove Chromium profile %s: %s", self.path, error)
                        return
                    await asyncio.sleep(CLEANUP_INTERVAL)
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None


def sweep_stale_profiles(root: Path | None = None) -> list[Path]:
    """Remove profile folders no running proxy holds (left by a proxy that was killed).
    Returns the folders removed."""
    base = Path(root or tempfile.gettempdir())
    removed = []
    for path in base.glob(PROFILE_PREFIX + "*"):
        try:
            if path.is_symlink() or not path.is_dir() or not _unheld(path):
                continue
            shutil.rmtree(path)
        except OSError as error:  # another user's folder, or one removed meanwhile
            logger.debug("Not removing %s: %s", path, error)
            continue
        removed.append(path)
    if removed:
        logger.info("Removed %d Chromium profile folders left by an earlier proxy", len(removed))
    return removed


def _unheld(path: Path) -> bool:
    try:
        fd = os.open(path / PROFILE_LOCK, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return time.time() - path.stat().st_mtime > UNLOCKED_PROFILE_AGE
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held by a running proxy: BlockingIOError
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


# The browser -----------------------------------------------------------------------------


@dataclass
class GovernedBrowser:
    """A running Chromium: Statelock's CDP connection and the agent's channel on its pipe."""

    process: asyncio.subprocess.Process
    multiplexer: CdpMultiplexer
    connection: CdpConnection
    profile: _Profile
    stderr: _StderrTail
    stderr_reader: asyncio.Task[None]
    stop_timeout: float = STOP_TIMEOUT

    async def close(self) -> None:
        await _teardown(
            self.process,
            self.multiplexer,
            self.connection,
            self.stderr_reader,
            self.profile,
            stop_timeout=self.stop_timeout,
        )


async def _teardown(
    process: asyncio.subprocess.Process | None,
    multiplexer: CdpMultiplexer | None,
    connection: CdpConnection | None,
    stderr_reader: asyncio.Task[None] | None,
    profile: _Profile,
    *,
    stop_timeout: float,
) -> None:
    """Close CDP, stop the process group, remove the profile; each step runs even if one before
    failed. Parts a failed launch never reached are None."""
    try:
        try:
            if connection is not None:
                await connection.close()
        finally:
            if multiplexer is not None:
                await multiplexer.close()
    finally:
        try:
            if process is not None:
                await stop_process(process, stop_timeout)
        finally:
            if stderr_reader is not None:
                stderr_reader.cancel()
                await asyncio.gather(stderr_reader, return_exceptions=True)
            await profile.remove()


class ChromiumCdpLauncher:
    """Starts a governed Chromium per session (``launch``): headless, with CDP over a pipe,
    a minimal environment and its own locked profile folder under ``profile_root``
    (default: the system temp folder).

    ``command_timeout`` bounds each of Statelock's own CDP commands; ``sweep_stale_profiles``
    removes the profile folders under ``profile_root`` that a killed proxy left behind.
    """

    def __init__(
        self,
        *,
        sandbox: bool = False,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        stop_timeout: float = STOP_TIMEOUT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        executable_path: Path | None = None,
        profile_root: Path | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.command_timeout = command_timeout
        self.profile_root = profile_root
        self._executable_path = executable_path

    async def executable_path(self) -> Path:
        """Playwright's bundled Chromium, resolved once."""
        if self._executable_path is None:
            async with async_playwright() as playwright:
                self._executable_path = Path(playwright.chromium.executable_path)
        return self._executable_path

    def sweep_stale_profiles(self) -> list[Path]:
        """Remove profile folders under profile_root that no running proxy holds."""
        return sweep_stale_profiles(self.profile_root)

    async def launch(self) -> GovernedBrowser:
        executable = await self.executable_path()
        if not executable.exists():
            raise BrowserLaunchError(f"Chromium executable not found: {executable}")
        profile = _Profile(Path(self.profile_root or tempfile.gettempdir()))
        args = [
            str(executable),
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile.path}",
            "about:blank",
        ]
        if not self.sandbox:
            args.insert(1, "--no-sandbox")

        stderr = _StderrTail()
        process: asyncio.subprocess.Process | None = None
        stderr_reader: asyncio.Task[None] | None = None
        multiplexer: CdpMultiplexer | None = None
        connection: CdpConnection | None = None
        try:
            process, read_fd, write_fd = await self._spawn(args)
            assert process.stderr is not None  # noqa: S101 - stderr=PIPE
            stderr_reader = asyncio.create_task(stderr.drain(process.stderr))
            multiplexer = CdpMultiplexer(await CdpPipe.open(read_fd, write_fd))
            multiplexer.start()
            connection = CdpConnection(multiplexer.statelock, command_timeout=self.command_timeout)
            connection.open()
            await self._wait_until_ready(connection, process, stderr_reader)
        except BaseException as error:
            # Undo whatever step the failed or cancelled launch reached.
            await _teardown(process, multiplexer, connection, stderr_reader, profile, stop_timeout=self.stop_timeout)
            if not isinstance(error, BrowserLaunchError):
                raise
            tail = stderr.text()
            hint = f" {SANDBOX_HINT}" if self.sandbox and "sandbox" in tail.lower() else ""
            detail = f" Chromium stderr: {tail}" if tail else ""
            raise BrowserLaunchError(f"{error}.{hint}{detail}") from error
        logger.info("Launched governed Chromium pid=%s (sandbox=%s)", process.pid, self.sandbox)
        return GovernedBrowser(
            process, multiplexer, connection, profile, stderr, stderr_reader, stop_timeout=self.stop_timeout
        )

    async def _spawn(self, args: list[str]) -> tuple[asyncio.subprocess.Process, int, int]:
        """Start Chromium; returns it and our ends of its CDP pipe (read, write)."""
        to_chromium = os.pipe()
        from_chromium = os.pipe()
        # The child's ends, above the fds it is given, so moving them to 3 and 4 overwrites neither.
        child_read = fcntl.fcntl(to_chromium[0], fcntl.F_DUPFD_CLOEXEC, FIRST_FREE_FD)
        child_write = fcntl.fcntl(from_chromium[1], fcntl.F_DUPFD_CLOEXEC, FIRST_FREE_FD)
        try:
            # close_fds=False: fds 3 and 4 are set up in the child, after the point where
            # close_fds would close them. Every other fd the proxy opens is close-on-exec.
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=chromium_environment(),
                start_new_session=True,
                close_fds=False,
                preexec_fn=_child_setup(child_read, child_write),
            )
        except BaseException:
            for fd in (*to_chromium, *from_chromium):
                os.close(fd)
            raise
        finally:
            os.close(child_read)
            os.close(child_write)
        os.close(to_chromium[0])
        os.close(from_chromium[1])
        return process, from_chromium[0], to_chromium[1]

    async def _wait_until_ready(
        self, connection: CdpConnection, process: asyncio.subprocess.Process, stderr_reader: asyncio.Task[None]
    ) -> None:
        """Chromium answers a first command within start_timeout, or the launch fails."""
        try:
            await connection.send("Browser.getVersion", timeout=self.start_timeout)
        except CdpError as error:
            # The pipe closed (Chromium exited) or nothing answered in time.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=EXIT_CODE_WAIT)
            if process.returncode is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(stderr_reader), timeout=EXIT_CODE_WAIT)
                raise BrowserLaunchError(f"Chromium exited with code {process.returncode} during startup") from error
            raise BrowserLaunchError(
                f"Chromium did not answer on its CDP pipe within {self.start_timeout:g} s "
                "(STATELOCK_CHROMIUM_START_TIMEOUT)"
            ) from error
