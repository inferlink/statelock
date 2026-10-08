import asyncio
import stat
from pathlib import Path

import pytest

from statelock.proxy.browser import BrowserLaunchError, ChromiumCdpLauncher, chromium_environment


def _fake_chromium(tmp_path: Path, message: str) -> Path:
    script = tmp_path / "chrome"
    script.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _launch(executable: Path, *, sandbox: bool) -> BrowserLaunchError:
    launcher = ChromiumCdpLauncher(sandbox=sandbox, executable_path=executable)
    with pytest.raises(BrowserLaunchError) as caught:
        asyncio.run(launcher.launch())
    return caught.value


def test_sandbox_failure_explains_what_to_do(tmp_path: Path) -> None:
    error = _launch(_fake_chromium(tmp_path, "FATAL: No usable sandbox! Update your kernel"), sandbox=True)
    assert "exited with code 1" in str(error)
    assert "README-sandbox.md" in str(error)
    assert "No usable sandbox" in str(error)


def test_other_launch_failures_include_stderr_without_the_sandbox_hint(tmp_path: Path) -> None:
    error = _launch(_fake_chromium(tmp_path, "cannot open display"), sandbox=False)
    assert "cannot open display" in str(error)
    assert "README-sandbox.md" not in str(error)


def _reporting_chromium(tmp_path: Path) -> Path:
    """Writes its environment, then reports a CDP endpoint in its profile as Chromium does."""
    script = tmp_path / "chrome"
    script.write_text(
        "#!/bin/sh\n"
        f"env > {tmp_path / 'env.txt'}\n"
        'for arg in "$@"; do case "$arg" in --user-data-dir=*) dir="${arg#--user-data-dir=}";; esac; done\n'
        'printf "45678\\n/devtools/browser/abc-123\\n" > "$dir/DevToolsActivePort"\n'
        "exec sleep 30\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_launch_connects_to_the_endpoint_its_own_browser_reports(tmp_path: Path, monkeypatch) -> None:
    # Port 0 and DevToolsActivePort: no port is chosen and released, so no other session's browser.
    monkeypatch.setenv("STATELOCK_PERCEPTION_API_KEY", "sk-not-for-chromium")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")

    async def scenario() -> str:
        browser = await ChromiumCdpLauncher(executable_path=_reporting_chromium(tmp_path)).launch()
        try:
            return browser.cdp_ws_url
        finally:
            await browser.close()

    assert asyncio.run(scenario()) == "ws://127.0.0.1:45678/devtools/browser/abc-123"
    environment = (tmp_path / "env.txt").read_text(encoding="utf-8")
    assert "STATELOCK_PERCEPTION_API_KEY" not in environment
    assert "LC_ALL=C.UTF-8" in environment and "PATH=" in environment


def test_chromium_environment_keeps_only_what_chromium_needs() -> None:
    environment = chromium_environment(
        {"HOME": "/h", "PATH": "/bin", "XDG_RUNTIME_DIR": "/run", "STATELOCK_API_KEY": "k", "AWS_SECRET": "s"}
    )
    assert environment == {"HOME": "/h", "PATH": "/bin", "XDG_RUNTIME_DIR": "/run"}
