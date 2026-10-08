"""compose.dev.yaml, OJS demo: its services point at files that exist and hold no password."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.dev.yaml"


@pytest.fixture
def services() -> dict[str, Any]:
    if not COMPOSE.exists():  # the Docker test image mounts only some files
        pytest.skip("compose.dev.yaml is not available")
    return dict(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def test_ojs_demo_services(services: dict[str, Any]) -> None:
    agents = ["ojs-agent", "ojs-record-agent", "ojs-block-agent"]
    assert set(services["ojs-mock"]["profiles"]) == set(agents)
    for name in agents:
        service = services[name]
        env = service["environment"]
        assert service["profiles"] == [name]
        assert env["STATELOCK_API_KEY"] == "slk_dev_ojs_screening_agent"
        assert not any("PASSWORD" in key for key in env)  # the agent never gets the password
        assert (ROOT / env["OJS_DECISIONS_FILE"].replace("/app/", "")).exists()
        assert service["depends_on"]["ojs-mock"]["condition"] == "service_healthy"
    assert services["ojs-record-agent"]["environment"]["OJS_RECORD_DECISIONS"] == "1"
    assert "OJS_DEMO_PROHIBITED_CLICK" in services["ojs-block-agent"]["environment"]
    assert "FROM dev AS ojs" in (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")


def test_the_proxy_loads_the_ojs_policy_and_rule(services: dict[str, Any]) -> None:
    from statelock.policy import PolicyEvaluator
    from statelock.policy.extensions import load_rule_modules

    env = dict(item.split("=", 1) for item in services["statelock"]["environment"])
    local = {
        key: [ROOT / part.replace("/app/", "") for part in env[key].split(",")] for key in env if "/app/" in env[key]
    }
    policies = [ROOT / "policies" / "default.yaml", *local["STATELOCK_EXTRA_POLICY_FILES"]]
    load_rule_modules(",".join(str(path) for path in local["STATELOCK_RULE_MODULES"]))
    evaluator = PolicyEvaluator.from_files(policies)
    assert {"ojs-journal", "ojs-login", "ojs-submissions", "ojs-workflow"} <= {p.policy_id for p in evaluator.policies}
