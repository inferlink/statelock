"""visual_assert end to end: the screenshot Statelock captures reaches the model (a fake here)."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from browser_support import (
    WAIT_MS,
    StatelockPolicyViolationError,
    actions,
    chromium_available,
    run_session,
    running_server,
)

from statelock.policy.perception import LiteLLMPerceptionEvaluator

pytestmark = pytest.mark.browser

POLICY = """
policies:
  - agent_id: visual_agent
    target_url_contains: /demo/finance
    pre_conditions:
      - visual_assert:
          check_name: amounts_match
          instruction: The bank deposit and the ERP invoice show the same amount.
          extract: {bank_deposit: string}
          cross_check:
            - {extracted: bank_deposit, field: bank_deposit_amount}
"""


class ScriptedModel:
    """litellm.acompletion stand-in: checks each request carries a JPEG, answers with `reply`."""

    def __init__(self) -> None:
        self.reply: Any = None
        self.images: list[bytes] = []

    async def __call__(self, **kwargs: Any) -> Any:
        url = kwargs["messages"][1]["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        self.images.append(base64.b64decode(url.split(",", 1)[1]))
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture(scope="module")
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture(scope="module")
def evaluator(model: ScriptedModel) -> LiteLLMPerceptionEvaluator:
    return LiteLLMPerceptionEvaluator("fake/vlm", completion=model, timeout=5)


@pytest.fixture(scope="module")
def visual_server(
    tmp_path_factory: pytest.TempPathFactory, evaluator: LiteLLMPerceptionEvaluator
) -> Iterator[dict[str, Any]]:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    with running_server(tmp_path_factory, policy=POLICY, agents=("visual_agent",), perception=evaluator) as running:
        yield running


@pytest.fixture(autouse=True)
def fresh_model(model: ScriptedModel, evaluator: LiteLLMPerceptionEvaluator) -> None:
    model.images = []
    evaluator._cache.clear()  # each test asks about its page afresh


async def _click(page: Any) -> str:
    await page.click("text=Mark as Paid")
    await page.wait_for_selector("text=Reconciliation complete", timeout=WAIT_MS)
    return "paid"


def test_a_passing_visual_check_allows_the_click(visual_server: dict[str, Any], model: ScriptedModel) -> None:
    model.reply, model.images = (
        {"passed": True, "reason": "both $5,000.00", "extracted": {"bank_deposit": "$5,000.00"}},
        [],
    )
    session_id, result = run_session(visual_server, "visual_agent", "/demo/finance?scenario=match", _click)
    assert result == "paid"
    assert len(model.images) == 1 and model.images[0][:2] == b"\xff\xd8"  # one real JPEG, for the press only
    press = next(r for r in actions(visual_server, session_id) if r["context"]["params"].get("type") == "mousePressed")
    assert press["verdict"]["decision"] == "allow"


def test_the_models_reading_is_cross_checked_against_the_page(
    visual_server: dict[str, Any], model: ScriptedModel
) -> None:
    # The model misreads (or is fooled by) the page: the DOM field says $4,900.00.
    model.reply = {"passed": True, "reason": "both $5,000.00", "extracted": {"bank_deposit": "$5,000.00"}}
    session_id, result = run_session(visual_server, "visual_agent", "/demo/finance?scenario=mismatch", _click)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "visual_assert"
    assert "bank_deposit_amount is '$4,900.00'" in result.reason
    blocked = [r for r in actions(visual_server, session_id) if r["verdict"]["decision"] == "block"]
    assert blocked[0]["verdict"]["evidence"]["cross_check"][0]["seen"] == "$5,000.00"


def test_unusable_model_output_fails_closed(visual_server: dict[str, Any], model: ScriptedModel) -> None:
    model.reply = "I think the amounts look fine!"
    _, result = run_session(visual_server, "visual_agent", "/demo/finance?scenario=match", _click)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "visual_assert" and "could not be evaluated" in result.reason


def test_without_a_model_visual_checks_block(tmp_path_factory: pytest.TempPathFactory) -> None:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    with running_server(tmp_path_factory, policy=POLICY, agents=("visual_agent",)) as running:
        _, result = run_session(running, "visual_agent", "/demo/finance?scenario=match", _click)
    assert isinstance(result, StatelockPolicyViolationError)
    assert "No perception evaluator is configured" in result.reason
