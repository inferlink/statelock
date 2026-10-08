"""Policy building blocks: field sources and read modes, triggered pre-conditions, the
page-text and fixed-value rules, fail-closed rule checks and custom rule modules."""

import asyncio
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from helpers import context_for, key_action, mouse_action, run
from pydantic import ValidationError

from statelock.core.enums import Decision
from statelock.policy import PolicyBundle, PolicyEvaluator, load_policy_bundle, parse_rule
from statelock.policy.evaluator import check_rule
from statelock.policy.extensions import load_rule_modules
from statelock.policy.fields import FieldSpec, ScopedField, resolve_fields, selector_specs
from statelock.policy.rules import RULES, Rule, RuleContext, RuleFailure, register_rule
from statelock.settings import Settings

URL = "http://journal.test/workflow/index/102/1"


def _evaluator(*policies: dict) -> PolicyEvaluator:
    return PolicyEvaluator(PolicyBundle.model_validate({"policies": list(policies)}))


# Field sources and read modes ------------------------------------------------------------------


def test_field_options_validation() -> None:
    FieldSpec(name="id", from_url=True, pattern=r"/(\d+)")
    FieldSpec(name="n", selector=".row", read="count", visible=True)
    FieldSpec(name="body", selector="body", frame="iframe#editor", read="length")
    for bad in (
        {"name": "a", "from_url": True, "selector": "#a"},
        {"name": "a", "from_url": True, "key": "k"},
        {"name": "a", "key": "k", "frame": "iframe"},
        {"name": "a", "key": "k", "visible": True},
        {"name": "a", "key": "k", "read": "count"},
        {"name": "a", "from_url": True, "read": "length"},
        {"name": "a", "selector": "#a", "read": "html"},
    ):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate(bad)


def test_from_url_field_and_selector_specs() -> None:
    fields = [
        ScopedField(FieldSpec(name="url_id", from_url=True, pattern=r"/workflow/index/(\d+)"), None),
        ScopedField(FieldSpec(name="rows", selector=".row", visible=True, read="count"), None),
        ScopedField(FieldSpec(name="email", selector="body", frame="iframe#e", read="length"), None),
    ]
    assert resolve_fields(fields, URL, {}, {"rows": "0", "email": "42"}) == {
        "url_id": "102",
        "rows": "0",  # a count of zero is a value, not a missing field
        "email": "42",
    }
    specs = {spec["name"]: spec for spec in selector_specs(fields[1:])}
    assert (specs["rows"]["visible"], specs["rows"]["read"], specs["rows"]["frame"]) == (True, "count", None)
    assert (specs["email"]["frame"], specs["email"]["read"]) == ("iframe#e", "length")


# Triggered pre-conditions ------------------------------------------------------------------------


TRIGGERED = {
    "agent_id": "agent",
    "pre_conditions": [
        {
            "trigger": {"click_text": ["Record"], "key": ["F9"]},
            "assert_compare": {"left": "n", "op": ">=", "value": 20},
        },
    ],
}


def test_triggered_pre_condition_runs_only_on_matching_activations() -> None:
    ev = _evaluator(TRIGGERED)
    short = {"n": "3"}

    def verdict(action, element):
        return run(ev.evaluate(context_for(action, element=element, extracted_fields=short))).decision

    assert verdict(mouse_action(), {"text": "Record Decision"}) == Decision.BLOCK
    assert verdict(mouse_action(), {"text": "Cancel"}) == Decision.ALLOW
    assert verdict(mouse_action("mouseMoved"), {"text": "Record Decision"}) == Decision.ALLOW  # not an activation
    assert verdict(mouse_action(), None) == Decision.BLOCK  # unresolved target: fail closed
    assert verdict(key_action(key="F9", code="F9"), None) == Decision.BLOCK
    assert verdict(key_action(), {"source": "focus", "text": "Record"}) == Decision.BLOCK
    assert verdict(key_action(key="a", code="KeyA"), {"source": "focus", "text": "Record"}) == Decision.ALLOW
    long = context_for(mouse_action(), element={"text": "Record"}, extracted_fields={"n": "25"})
    assert run(ev.evaluate(long)).decision == Decision.ALLOW


def test_post_conditions_are_rules_with_optional_triggers() -> None:
    ev = _evaluator({"agent_id": "agent", "post_conditions": [{"require_page_text": {"values": ["Saved"]}}]})
    [rule] = ev.policies[0].post_conditions
    assert (rule.rule_name, rule.trigger) == ("require_page_text", None)
    with pytest.raises(ValidationError):
        _evaluator({"agent_id": "agent", "pre_conditions": [{"trigger": {}, "require_page_text": {"values": ["x"]}}]})


# Rules --------------------------------------------------------------------------------------------


def test_prohibit_page_text() -> None:
    ev = _evaluator({"agent_id": "agent", "pre_conditions": [{"prohibit_page_text": {"values": ["Invalid password"]}}]})
    shown = context_for(mouse_action(), element={"text": "x"}, page_text="Error: invalid PASSWORD")
    verdict = run(ev.evaluate(shown))
    assert (verdict.decision, verdict.rule) == (Decision.BLOCK, "prohibit_page_text")
    fine = context_for(mouse_action(), element={"text": "x"}, page_text="Welcome")
    assert run(ev.evaluate(fine)).decision == Decision.ALLOW
    with pytest.raises(ValueError):
        parse_rule({"prohibit_page_text": {"values": []}}, "pre")


def test_assert_field_equal_with_a_fixed_value() -> None:
    ev = _evaluator(
        {"agent_id": "agent", "pre_conditions": [{"assert_field_equal": {"left": "user", "value": "editor"}}]}
    )
    wrong = context_for(mouse_action(), element={"text": "x"}, extracted_fields={"user": "admin"})
    assert run(ev.evaluate(wrong)).decision == Decision.BLOCK
    right = context_for(mouse_action(), element={"text": "x"}, extracted_fields={"user": " Editor "})
    assert run(ev.evaluate(right)).decision == Decision.ALLOW
    for options in ({"left": "user"}, {"left": "user", "right": "other", "value": "editor"}):
        with pytest.raises(ValueError):
            parse_rule({"assert_field_equal": options}, "pre")


# Fail-closed rule checks and custom rules ----------------------------------------------------------


class _Raises(Rule):
    rule_name: ClassVar[str] = "test_raises"

    async def check(self, ctx: RuleContext) -> RuleFailure | None:  # noqa: ARG002
        raise RuntimeError("source unavailable")


class _Slow(Rule):
    rule_name: ClassVar[str] = "test_slow"
    check_timeout: ClassVar[float | None] = 0.05

    async def check(self, ctx: RuleContext) -> RuleFailure | None:  # noqa: ARG002
        await asyncio.sleep(5)
        return None


def _rule_context() -> RuleContext:
    from statelock.policy import NotConfiguredPerceptionEvaluator

    ctx = context_for(mouse_action(), element={"text": "x"})
    return RuleContext(ctx, ctx.browser_state, "pre", NotConfiguredPerceptionEvaluator())


def test_check_rule_fails_closed() -> None:
    raised = run(check_rule(_Raises(), _rule_context()))
    assert raised is not None
    assert "could not be evaluated: RuntimeError: source unavailable" in raised.reason
    slow = run(check_rule(_Slow(), _rule_context()))
    assert slow is not None
    assert "did not answer within 0.05 s" in slow.reason


RULE_SOURCE = """
from typing import ClassVar
from statelock.policy.rules import Rule, RuleFailure, register_rule

@register_rule
class {cls}(Rule):
    rule_name: ClassVar[str] = "{name}"
    blocked: str

    async def check(self, ctx):
        text = (ctx.state.page_text or "") if ctx.state else ""
        return RuleFailure("custom block") if self.blocked in text else None
"""


def _write_rule(directory: Path, module: str, name: str) -> Path:
    path = directory / f"{module}.py"
    path.write_text(RULE_SOURCE.format(cls="Custom", name=name), encoding="utf-8")
    return path


def test_load_rule_modules_from_a_file_and_a_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    by_path = _write_rule(tmp_path, "by_path", "test_custom_by_path")
    _write_rule(tmp_path, "by_name_rules", "test_custom_by_name")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        loaded = load_rule_modules(f" {by_path} , by_name_rules,")
        assert loaded[-2:] == [str(by_path), "by_name_rules"]
        assert load_rule_modules(str(by_path))[-1] == str(by_path)  # loading again is a no-op
        ev = _evaluator(
            {"agent_id": "agent", "pre_conditions": [{"test_custom_by_path": {"blocked": "Danger"}}]},
            {"agent_id": "agent", "pre_conditions": [{"test_custom_by_name": {"blocked": "Other"}}]},
        )
        ctx = context_for(mouse_action(), element={"text": "x"}, page_text="Danger zone")
        verdict = run(ev.evaluate(ctx))
        assert (verdict.rule, verdict.reason, verdict.policy_id) == ("test_custom_by_path", "custom block", "agent#1")
    finally:
        for name in ("test_custom_by_path", "test_custom_by_name"):
            RULES.pop(name, None)
        sys.modules.pop("by_name_rules", None)
    with pytest.raises(FileNotFoundError):
        load_rule_modules(str(tmp_path / "missing.py"))


def test_unknown_rule_names_where_custom_rules_come_from() -> None:
    with pytest.raises(ValueError, match="STATELOCK_RULE_MODULES"):
        parse_rule({"no_such_rule": {}}, "pre")


def test_a_rule_name_cannot_be_taken_twice() -> None:
    class Duplicate(Rule):
        rule_name: ClassVar[str] = "require_page_text"

        async def check(self, ctx: RuleContext) -> RuleFailure | None:  # noqa: ARG002
            return None

    with pytest.raises(ValueError, match="already registered"):
        register_rule(Duplicate)


# Several policy files ------------------------------------------------------------------------------


def test_policy_files_load_as_one_bundle(tmp_path: Path) -> None:
    first, second = tmp_path / "default.yaml", tmp_path / "example.yaml"
    first.write_text("policies:\n  - {id: base, agent_id: a}\n", encoding="utf-8")
    second.write_text("id: extra\nagent_id: b\n", encoding="utf-8")
    bundle = load_policy_bundle(first, second)
    assert [p.policy_id for p in bundle.policies] == ["base", "extra"]
    assert PolicyEvaluator.from_files([first, second]).registered_agent_ids == {"a", "b"}
    second.write_text("id: base\nagent_id: b\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="base"):
        load_policy_bundle(first, second)


def test_settings_policy_files() -> None:
    settings = Settings(policy_file=Path("/p/default.yaml"), extra_policy_files=" /p/a.yaml, ,/p/b.yaml ")
    assert settings.policy_files == [Path("/p/default.yaml"), Path("/p/a.yaml"), Path("/p/b.yaml")]
    assert Settings(policy_file=Path("/p/default.yaml")).policy_files == [Path("/p/default.yaml")]
