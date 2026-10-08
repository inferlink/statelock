"""visual_assert and the litellm perception evaluator, with a fake model (no network)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import context_for, key_action, mouse_action, run
from pydantic import ValidationError

from statelock.core.enums import Decision
from statelock.core.state import BrowserState
from statelock.policy.evaluator import PolicyEvaluator
from statelock.policy.models import PolicyBundle
from statelock.policy.perception import (
    LiteLLMPerceptionEvaluator,
    PerceptionRequest,
    check_local_endpoint,
    parse_output,
    response_schema,
)
from statelock.services import perception_evaluator
from statelock.settings import Settings

SHOT = base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()


class FakeModel:
    """Stands in for litellm.acompletion: returns the queued outputs, records the calls."""

    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        if output == "hang":
            await asyncio.sleep(10)
        content = output if isinstance(output, str) else json.dumps(output)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _request(extract: dict[str, str] | None = None, shot: str | None = SHOT) -> PerceptionRequest:
    state = BrowserState(url="https://erp.test/pay", screenshot_base64=shot)
    return PerceptionRequest(
        check_name="c",
        instruction="Amounts match.",
        context=context_for(mouse_action(), state=state),
        state=state,
        extract=extract or {},
    )


def _evaluator(model: FakeModel, **options: Any) -> LiteLLMPerceptionEvaluator:
    return LiteLLMPerceptionEvaluator("hosted_vllm/test-vlm", completion=model, **{"timeout": 1.0, **options})


def test_valid_output_passes_and_the_request_is_strict() -> None:
    model = FakeModel({"passed": True, "reason": "ok", "extracted": {"amount": "$5,000.00"}})
    verdict = run(_evaluator(model).evaluate(_request({"amount": "string"})))
    assert (verdict.passed, verdict.extracted) == (True, {"amount": "$5,000.00"})
    assert verdict.meta["model"] == "hosted_vllm/test-vlm" and verdict.meta["attempts"] == 1
    call = model.calls[0]
    assert call["temperature"] == 0
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["response_format"]["json_schema"]["schema"] == response_schema({"amount": "string"})
    image = call["messages"][1]["content"][1]["image_url"]["url"]
    assert image == f"data:image/jpeg;base64,{SHOT}"


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        {"passed": "yes", "reason": "r", "extracted": {}},  # a string is not a boolean (strict)
        {"passed": True, "reason": "r", "extracted": {}, "extra": 1},  # unknown key
        {"passed": True, "reason": "r"},  # extracted missing
        {"passed": True, "reason": "r", "extracted": {"amount": 5}},  # wrong type
    ],
)
def test_invalid_output_is_retried_then_fails_closed(bad: Any) -> None:
    model = FakeModel(bad, bad)
    verdict = run(_evaluator(model, retries=1).evaluate(_request({"amount": "string"})))
    assert verdict.passed is False
    assert verdict.meta["failed_closed"] is True
    assert verdict.meta["attempts"] == 2


def test_a_retry_can_recover() -> None:
    model = FakeModel("garbage", {"passed": False, "reason": "amounts differ", "extracted": {}})
    verdict = run(_evaluator(model).evaluate(_request()))
    assert (verdict.passed, verdict.reason, verdict.meta["attempts"]) == (False, "amounts differ", 2)
    assert not verdict.meta.get("failed_closed")


def test_errors_timeouts_and_missing_screenshots_fail_closed() -> None:
    assert run(_evaluator(FakeModel(RuntimeError("connection refused"))).evaluate(_request())).meta["failed_closed"]
    slow = run(_evaluator(FakeModel("hang"), timeout=0.05, retries=0).evaluate(_request()))
    assert slow.passed is False and "timed out" in slow.reason
    no_shot = run(_evaluator(FakeModel()).evaluate(_request(shot=None)))
    assert no_shot.passed is False and "No screenshot" in no_shot.reason


def test_same_page_and_question_is_asked_once() -> None:
    model = FakeModel({"passed": True, "reason": "ok", "extracted": {}})
    evaluator = _evaluator(model)
    run(evaluator.evaluate(_request()))
    second = run(evaluator.evaluate(_request()))
    assert len(model.calls) == 1 and second.meta["cached"] is True


def test_fenced_json_is_accepted() -> None:
    assert parse_output('```json\n{"passed": true, "reason": "r", "extracted": {}}\n```', {}).passed


def _policy_evaluator(rule: dict[str, Any], model: FakeModel) -> PolicyEvaluator:
    bundle = PolicyBundle.model_validate(
        {"policies": [{"agent_id": "agent", "pre_conditions": [{"visual_assert": rule}]}]}
    )
    return PolicyEvaluator(bundle, _evaluator(model))


def _context(action: Any, shot: str = SHOT, **fields: str) -> Any:
    state = BrowserState(url="https://erp.test/pay", screenshot_base64=shot, extracted_fields=fields)
    return context_for(action, state=state)


RULE = {
    "check_name": "amounts",
    "instruction": "The bank deposit and the ERP invoice show the same amount.",
    "extract": {"invoice": "string"},
    "cross_check": [{"extracted": "invoice", "field": "erp_invoice"}],
}


def test_visual_assert_checks_activations_only_by_default() -> None:
    model = FakeModel()
    evaluator = _policy_evaluator(RULE, model)
    assert run(evaluator.evaluate(_context(mouse_action("mouseMoved")))).decision == Decision.ALLOW
    assert run(evaluator.evaluate(_context(key_action("keyDown", key="a", code="KeyA")))).decision == Decision.ALLOW
    assert model.calls == []  # no model call for moves and plain typing


def test_visual_assert_cross_checks_the_models_reading() -> None:
    model = FakeModel(
        {"passed": True, "reason": "match", "extracted": {"invoice": "$5,000.00"}},
        {"passed": True, "reason": "match", "extracted": {"invoice": "$5,300.00"}},
    )
    evaluator = _policy_evaluator(RULE, model)
    assert run(evaluator.evaluate(_context(mouse_action(), erp_invoice="5000"))).decision == Decision.ALLOW
    blocked = run(evaluator.evaluate(_context(mouse_action(), shot=SHOT + "AA", erp_invoice="5000")))
    assert blocked.decision == Decision.BLOCK
    assert "the model read invoice='$5,300.00', but erp_invoice is '5000'" in blocked.reason


def test_visual_assert_failure_blocks_with_the_models_reason() -> None:
    evaluator = _policy_evaluator(
        {**RULE, "cross_check": []},
        FakeModel({"passed": False, "reason": "invoice is 5,300", "extracted": {"invoice": None}}),
    )
    verdict = run(evaluator.evaluate(_context(mouse_action())))
    assert verdict.decision == Decision.BLOCK and verdict.rule == "visual_assert"
    assert "invoice is 5,300" in verdict.reason
    assert verdict.evidence["perception"]["meta"]["model"] == "hosted_vllm/test-vlm"


def test_cross_check_must_name_an_extracted_value() -> None:
    with pytest.raises(ValidationError, match="cross_check reads values"):
        PolicyBundle.model_validate(
            {"policies": [{"agent_id": "a", "pre_conditions": [{"visual_assert": {**RULE, "extract": {}}}]}]}
        )


def test_perception_is_configured_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from statelock import services

    configured: dict[str, Any] = {}

    class StubEvaluator:
        def __init__(self, model: str, **options: Any) -> None:
            configured.update({"model": model, **options})

    monkeypatch.setattr(services, "LiteLLMPerceptionEvaluator", StubEvaluator)
    assert perception_evaluator(Settings()) is None
    evaluator = perception_evaluator(
        Settings(
            perception_model="ollama_chat/qwen2.5vl:7b", perception_api_base="http://gpu:11434", perception_timeout=5
        )
    )
    assert isinstance(evaluator, StubEvaluator)
    assert configured["model"] == "ollama_chat/qwen2.5vl:7b"
    assert configured["api_base"] == "http://gpu:11434"
    assert configured["timeout"] == 5


def test_perception_check_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    from statelock import __main__ as cli

    image = tmp_path / "shot.jpg"
    image.write_bytes(b"\xff\xd8\xff fake")
    model = FakeModel({"passed": False, "reason": "differ", "extracted": {"amount": 4900}})
    monkeypatch.setattr("statelock.services.perception_evaluator", lambda _settings: _evaluator(model))
    assert cli.main(["perception-check", str(image), "-i", "Amounts match.", "-x", "amount=number"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["extracted"] == {"amount": 4900} and printed["reason"] == "differ"
    assert model.calls[0]["response_format"]["json_schema"]["schema"]["properties"]["extracted"]["required"] == [
        "amount"
    ]
    monkeypatch.setattr("statelock.services.perception_evaluator", lambda _settings: None)
    assert cli.main(["perception-check", str(image), "-i", "x"]) == 2


def test_perception_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(perception_retries=-1)
    with pytest.raises(ValidationError):
        Settings(perception_timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        LiteLLMPerceptionEvaluator("m", completion=FakeModel(), retries=-1)


def _resolver(*addresses: str):
    def resolve(host: str, port: None) -> list[tuple[Any, ...]]:
        if not addresses:
            raise OSError(f"no such host {host}")
        return [(None, None, None, "", (address, 0)) for address in addresses]

    return resolve


@pytest.mark.parametrize(
    ("api_base", "resolved"),
    [
        ("http://localhost:11434", ("127.0.0.1", "::1")),
        ("http://127.0.0.1:11434", ()),
        ("http://10.0.4.7:8000/v1", ()),
        ("http://[::1]:8000/v1", ()),
        ("http://[fd12:3456::7]:8000/v1", ()),
        ("http://[::ffff:10.1.2.3]/v1", ()),
        ("http://100.101.102.103:11434", ()),  # Tailscale
        ("http://gpu:8000/v1", ("172.18.0.5",)),  # a Docker service name
        ("https://vlm.internal:443/v1", ("192.168.1.20",)),
    ],
)
def test_local_only_accepts_servers_on_this_machine_or_a_private_network(
    api_base: str, resolved: tuple[str, ...]
) -> None:
    lookups: list[str] = []
    resolve = _resolver(*resolved)

    def recording(host: str, port: None) -> list[tuple[Any, ...]]:
        lookups.append(host)
        return resolve(host, port)

    assert check_local_endpoint("ollama_chat/qwen2.5vl:7b", api_base, resolve=recording) is None
    assert len(lookups) == (1 if resolved else 0)  # host names are resolved, addresses are not


@pytest.mark.parametrize(
    ("api_base", "resolved", "message"),
    [
        (None, (), "must be named"),
        ("", (), "must be named"),
        ("ftp://10.0.0.1/v1", (), "not an http"),
        ("http://8.8.8.8/v1", (), "resolves to 8.8.8.8"),
        ("https://generativelanguage.googleapis.com", ("142.250.80.10",), "not on this machine"),
        ("http://mixed.example:8000", ("10.0.0.2", "203.0.113.9"), "203.0.113.9"),  # one public address is enough
        ("http://0.0.0.0:11434", (), "not on this machine"),
        ("http://[::ffff:8.8.8.8]/v1", (), "not on this machine"),
        ("http://nowhere.invalid", (), "cannot resolve"),
    ],
)
def test_local_only_refuses_anything_else(api_base: str | None, resolved: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_local_endpoint("ollama_chat/qwen2.5vl:7b", api_base, resolve=_resolver(*resolved))


def test_local_only_is_checked_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted model with STATELOCK_PERCEPTION_LOCAL_ONLY never starts; a local one does,
    without litellm's model price list download."""
    monkeypatch.setenv("STATELOCK_PERCEPTION_LOCAL_ONLY", "true")
    monkeypatch.setenv("STATELOCK_PERCEPTION_MODEL", "gemini/gemini-2.5-flash")
    assert Settings().perception_local_only is True
    with pytest.raises(ValueError, match="not a self-hosted model"):
        perception_evaluator(Settings())
    # A hosted provider pointed at a local address is still refused: its prefix decides the route.
    with pytest.raises(ValueError, match="not a self-hosted model"):
        check_local_endpoint("gemini/gemini-2.5-flash", "http://127.0.0.1:8000")
    with pytest.raises(ValueError, match="not a self-hosted model"):
        check_local_endpoint("qwen2.5vl", "http://127.0.0.1:11434")
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    evaluator = LiteLLMPerceptionEvaluator(
        "ollama_chat/qwen2.5vl:7b", api_base="http://127.0.0.1:11434", local_only=True, completion=FakeModel()
    )
    assert evaluator.local_only
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_without_local_only_a_hosted_model_is_allowed() -> None:
    evaluator = LiteLLMPerceptionEvaluator("gemini/gemini-2.5-flash", completion=FakeModel())
    assert not evaluator.local_only
