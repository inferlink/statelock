"""A real vision-language model on the fixture screenshots (tests/fixtures/perception).

Runs only when STATELOCK_PERCEPTION_MODEL is set, and prints the verdict, what the model
read and the latency for each fixture. For example (or the perception-tests service in
compose.dev.yaml):

    STATELOCK_PERCEPTION_MODEL=ollama_chat/qwen2.5vl:7b \\
    STATELOCK_PERCEPTION_API_BASE=http://localhost:11434 pytest tests/test_perception_model.py

Whether the model answers in the expected form is always checked. Whether it judges and
reads each fixture correctly is checked too, unless PERCEPTION_CHECK_ANSWERS=false: the
Compose demo sets that for the small default model, whose answers are only printed (a
3B model reads the amounts but can misjudge them).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest
from helpers import context_for, mouse_action, run

from statelock.core.state import BrowserState
from statelock.policy.perception import PerceptionRequest
from statelock.policy.rules import values_equal
from statelock.services import perception_evaluator
from statelock.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "perception"
SPEC = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))

CHECK_ANSWERS = os.environ.get("PERCEPTION_CHECK_ANSWERS", "true").strip().lower() != "false"

needs_model = pytest.mark.skipif(
    not os.environ.get("STATELOCK_PERCEPTION_MODEL"), reason="set STATELOCK_PERCEPTION_MODEL to test a real model"
)


def test_fixture_files_exist() -> None:
    for case in SPEC["cases"]:
        assert (FIXTURES / case["image"]).read_bytes()[:2] == b"\xff\xd8"


@needs_model
@pytest.mark.parametrize("case", SPEC["cases"], ids=lambda case: case["image"])
def test_model_judges_the_fixture(case: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    evaluator = perception_evaluator(Settings())
    assert evaluator is not None
    shot = base64.b64encode((FIXTURES / case["image"]).read_bytes()).decode()
    state = BrowserState(url="http://localhost/demo/finance", screenshot_base64=shot)
    request = PerceptionRequest(
        check_name="fixture",
        instruction=SPEC["check"]["instruction"],
        extract=SPEC["check"]["extract"],
        context=context_for(mouse_action(), state=state),
        state=state,
    )
    verdict = run(evaluator.evaluate(request))
    with capsys.disabled():  # one line per fixture: the numbers for comparing models
        print(
            f"\n  {verdict.meta.get('model')} {case['image']}: passed={verdict.passed} (expected {case['passed']}), "
            f"read={json.dumps(verdict.extracted)}, {verdict.meta.get('latency_ms')} ms, "
            f"attempts={verdict.meta.get('attempts')}" + ("" if CHECK_ANSWERS else " (answer not checked)")
        )
    # The model answered, in the expected form.
    assert not verdict.meta.get("failed_closed"), verdict.meta
    assert set(case["extracted"]) <= set(verdict.extracted), verdict.extracted
    if not CHECK_ANSWERS:
        return
    assert verdict.passed is case["passed"], verdict.reason
    for name, expected in case["extracted"].items():
        assert values_equal(str(verdict.extracted.get(name)), expected), (name, verdict.extracted)
