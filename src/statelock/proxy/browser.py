# SPDX-License-Identifier: Apache-2.0
"""Launch the governed Chromium with a raw CDP endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

CDP_READY_ATTEMPTS = 50
CDP_READY_INTERVAL = 0.1
TERMINATE_TIMEOUT = 5.0
CLEANUP_ATTEMPTS = 10
CLEANUP_INTERVAL = 0.2
STDERR_TAIL_BYTES = 2000
# Chromium writes "<port>\n/devtools/browser/<id>" here, in its own profile, once CDP listens.
ACTIVE_PORT_FILE = "DevToolsActivePort"
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


def _stderr_tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


def chromium_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment Chromium is started with: only what it needs from the proxy's."""
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name in CHROMIUM_ENV_NAMES or name.startswith(CHROMIUM_ENV_PREFIXES)
    }


def _read_active_port(path: Path) -> tuple[int, str] | None:
    """(port, browser path) from DevToolsActivePort, or None until Chromium has written both lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].strip().isdigit() or not lines[1].startswith("/devtools/browser/"):
        return None
    return int(lines[0].strip()), lines[1].strip()


async def _remove_dir(directory: tempfile.TemporaryDirectory[str]) -> None:
    """Remove a temporary directory. Chromium's helper processes can still write into the
    profile for a moment after the main process exits, so retry before giving up."""
    for attempt in range(CLEANUP_ATTEMPTS):
        try:
            await asyncio.to_thread(directory.cleanup)
            return
        except OSError as error:
            if attempt == CLEANUP_ATTEMPTS - 1:
                logger.warning("Could not fully clean Chromium directory %s: %s", directory.name, error)
                return
            await asyncio.sleep(CLEANUP_INTERVAL)


class BrowserLaunchError(RuntimeError):
    pass


@dataclass
class GovernedBrowser:
    process: asyncio.subprocess.Process
    cdp_ws_url: str
    user_data_dir: tempfile.TemporaryDirectory[str]
    log_dir: tempfile.TemporaryDirectory[str] | None = None

    async def close(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=TERMINATE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Chromium did not terminate cleanly; killing process")
                self.process.kill()
                await self.process.wait()
        for directory in (self.user_data_dir, self.log_dir):
            if directory is not None:
                await _remove_dir(directory)


class ChromiumCdpLauncher:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        sandbox: bool = False,
        executable_path: Path | None = None,
    ) -> None:
        self.host = host
        self.sandbox = sandbox
        self._executable_path = executable_path

    async def executable_path(self) -> Path:
        """Playwright's bundled Chromium, resolved once."""
        if self._executable_path is None:
            async with async_playwright() as playwright:
                self._executable_path = Path(playwright.chromium.executable_path)
        return self._executable_path

    async def launch(self) -> GovernedBrowser:
        executable = await self.executable_path()
        if not executable.exists():
            raise BrowserLaunchError(f"Chromium executable not found: {executable}")

        user_data_dir = tempfile.TemporaryDirectory(prefix="statelock-chromium-")
        # Port 0: the OS picks a free port, and Chromium reports it in its own profile,
        # so the browser connected to is always the one launched here.
        args = [
            str(executable),
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-address={self.host}",
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir.name}",
            "about:blank",
        ]
        if not self.sandbox:
            args.insert(1, "--no-sandbox")

        # Chromium's stderr goes to a file, so a launch failure can say why (e.g. no usable sandbox).
        log_dir = tempfile.TemporaryDirectory(prefix="statelock-chromium-log-")
        stderr_path = Path(log_dir.name) / "stderr.log"
        process: asyncio.subprocess.Process | None = None
        try:
            with stderr_path.open("wb") as stderr:
                process = await asyncio.create_subprocess_exec(
                    *args, stdout=subprocess.DEVNULL, stderr=stderr, env=chromium_environment()
                )
            cdp_ws_url = await self._wait_for_cdp_url(Path(user_data_dir.name) / ACTIVE_PORT_FILE, process)
        except BaseException as error:
            if process is not None:
                await self._stop(process)
            tail = _stderr_tail(stderr_path)  # before its folder is removed
            for directory in (user_data_dir, log_dir):
                await _remove_dir(directory)
            if not isinstance(error, BrowserLaunchError):
                raise
            hint = f" {SANDBOX_HINT}" if self.sandbox and "sandbox" in tail.lower() else ""
            detail = f" Chromium stderr: {tail}" if tail else ""
            raise BrowserLaunchError(f"{error}.{hint}{detail}") from error
        logger.info("Launched governed Chromium on %s (sandbox=%s)", cdp_ws_url, self.sandbox)
        return GovernedBrowser(process=process, cdp_ws_url=cdp_ws_url, user_data_dir=user_data_dir, log_dir=log_dir)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.terminate()
        await process.wait()

    async def _wait_for_cdp_url(self, active_port_file: Path, process: asyncio.subprocess.Process) -> str:
        for _ in range(CDP_READY_ATTEMPTS):
            if process.returncode is not None:
                raise BrowserLaunchError(f"Chromium exited with code {process.returncode} during startup")
            active = await asyncio.to_thread(_read_active_port, active_port_file)
            if active is not None:
                port, browser_path = active
                return f"ws://{self.host}:{port}{browser_path}"
            await asyncio.sleep(CDP_READY_INTERVAL)
        raise BrowserLaunchError(f"Timed out waiting for Chromium to report its CDP endpoint in {active_port_file}")
