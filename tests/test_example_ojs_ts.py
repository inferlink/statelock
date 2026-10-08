"""The TypeScript OJS example (examples/ojs-ts): Stagehand v3 and the JS SDK through Statelock.

Skipped unless Node and the example's packages are installed:
    (cd js && npm install && npm run build) && (cd examples/ojs-ts && npm install)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ojs-ts"
if not (EXAMPLE / "node_modules").is_dir() or not (ROOT / "js" / "dist").is_dir() or not shutil.which("npx"):
    pytest.skip("examples/ojs-ts is not installed (npm install)", allow_module_level=True)
sys.path.insert(0, str(ROOT / "examples" / "ojs"))

import mock_ojs  # noqa: E402
from browser_support import actions, agent_key, chromium_available, running_server  # noqa: E402
from ojs_support import RULE_MODULE, ojs_policy  # noqa: E402

pytestmark = pytest.mark.browser
AGENT = "ojs_screening_agent"


@pytest.fixture(scope="module")
def ojs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    policy = ojs_policy(ROOT / "examples" / "ojs" / "demo_decisions.json")
    secrets = (("ojs_password", [AGENT], mock_ojs.JOURNAL + "/login", mock_ojs.PASSWORD),)
    routers = (mock_ojs.build_router(mock_ojs.MockJournal.sample()),)
    with running_server(
        tmp_path_factory, policy=policy, agents=(AGENT,), routers=routers, secrets=secrets, rule_modules=RULE_MODULE
    ) as server:
        yield server


def _run(server: dict[str, Any], out: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "STATELOCK_URL": server["base"],
        "STATELOCK_API_KEY": agent_key(AGENT),
        "OJS_BASE_URL": server["base"] + mock_ojs.JOURNAL,
        "OJS_USERNAME": mock_ojs.USERNAME,
        "OJS_OUTPUT_DIR": str(out),
        **extra,
    }
    return subprocess.run(
        ["npx", "tsx", "ojs_agent.ts"], cwd=EXAMPLE, env=env, capture_output=True, text=True, timeout=180, check=False
    )


def test_typescript_agent_screens_papers_and_reuses_its_login(ojs: dict[str, Any], tmp_path: Path) -> None:
    first = _run(ojs, tmp_path, OJS_SAVED_SESSION="ts-test")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Logged in as editor" in first.stdout
    checks = {s["paperId"]: s["checks"] for s in json.loads((tmp_path / "results.json").read_text())}
    assert checks[101] == {"has_manuscript_pdf": True, "has_comments_to_editor": True}
    assert checks[102]["has_comments_to_editor"] is False
    assert checks[103]["has_manuscript_pdf"] is False  # paper.tex

    second = _run(ojs, tmp_path, OJS_SAVED_SESSION="ts-test")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Already logged in (saved session)" in second.stdout


def test_typescript_agent_prohibited_click_ends_the_session(ojs: dict[str, Any], tmp_path: Path) -> None:
    result = _run(ojs, tmp_path, OJS_SAVED_SESSION="", OJS_DEMO_PROHIBITED_CLICK="Send to Review")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Statelock stopped the agent: prohibit_click_text" in result.stderr
    evidence = "".join(p.read_text(errors="replace") for p in (ojs["root"] / "artifacts").rglob("context.json"))
    assert mock_ojs.PASSWORD not in evidence
    assert any(
        r["context"]["params"].get("statelock_secrets")
        for s in (ojs["root"] / "artifacts" / "sessions").iterdir()
        for r in actions(ojs, s.name)
    )
