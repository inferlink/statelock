"""The OJS example's policy and custom rule, for the Python and TypeScript OJS tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ojs"
RULE_MODULE = str(EXAMPLE / "ojs_rules.py")
DECISION_RULE = "ojs_decision: {paper_field: url_paper_id"


def ojs_policy(decisions_file: Path) -> str:
    """examples/ojs/ojs_policy.yaml, with the decision rule reading ``decisions_file``
    (the editors' decisions as Statelock sees them; a test may make them differ from the agent's)."""
    policy = (EXAMPLE / "ojs_policy.yaml").read_text(encoding="utf-8")
    assert DECISION_RULE in policy
    return policy.replace(
        DECISION_RULE, f"ojs_decision: {{decisions_file: {json.dumps(str(decisions_file))}, paper_field: url_paper_id"
    )


def write_decisions(path: Path, decisions: dict[int, dict[str, Any]]) -> None:
    path.write_text(json.dumps({str(paper): decision for paper, decision in decisions.items()}), encoding="utf-8")
