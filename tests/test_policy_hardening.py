"""Policy edge cases that must fail closed: touch input, text and amount comparison,
URL scopes, field sources, and rule options that would otherwise do nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import context_for, run
from pydantic import ValidationError

from statelock.core.actions import TOUCH_METHOD, ActionKind, CdpAction, classify_cdp_action
from statelock.core.enums import Decision
from statelock.core.state import BrowserState
from statelock.core.urls import page_address, url_in_scope
from statelock.policy import PolicyBundle, PolicyConfig, PolicyEvaluator, load_policy_bundle, parse_rule
from statelock.policy.fields import parse_number
from statelock.policy.rules import values_equal


def evaluator(*policies: dict[str, Any]) -> PolicyEvaluator:
    return PolicyEvaluator(PolicyBundle.model_validate({"policies": list(policies)}))


def touch(event_type: str, session_id: str = "S1") -> CdpAction:
    points = [] if event_type == "touchEnd" else [{"x": 10, "y": 20}]
    action = classify_cdp_action(
        {
            "id": 7,
            "method": TOUCH_METHOD,
            "params": {"type": event_type, "touchPoints": points},
            "sessionId": session_id,
        }
    )
    assert action is not None
    return action


# Touch input (Playwright tap) ------------------------------------------------------------


def test_touch_start_and_end_are_an_activation_and_its_commit() -> None:
    start, move, end = touch("touchStart"), touch("touchMove"), touch("touchEnd")
    assert start.kind == ActionKind.TOUCH
    assert start.starts_activation and not start.ends_activation and not start.is_commit
    assert end.ends_activation and end.is_commit and not end.starts_activation
    assert not (move.starts_activation or move.ends_activation or move.is_commit)
    # An approved touchStart covers its touchEnd (one tap, one review), in the same tab only.
    assert start.release_key == end.release_key
    assert start.release_key != touch("touchEnd", session_id="S2").release_key


PROHIBIT_DELETE = {"agent_id": "agent", "pre_conditions": [{"prohibit_click_text": {"values": ["Delete"]}}]}


def test_touch_on_prohibited_text_is_blocked() -> None:
    verdict = run(evaluator(PROHIBIT_DELETE).evaluate(context_for(touch("touchStart"), element={"text": "Delete"})))
    assert verdict.decision == Decision.BLOCK
    assert "prohibited value: Delete" in verdict.reason
    allowed = run(evaluator(PROHIBIT_DELETE).evaluate(context_for(touch("touchStart"), element={"text": "Save"})))
    assert allowed.decision == Decision.ALLOW


def test_touch_with_an_unresolved_target_is_blocked() -> None:
    verdict = run(evaluator(PROHIBIT_DELETE).evaluate(context_for(touch("touchStart"), element=None)))
    assert verdict.decision == Decision.BLOCK
    assert "could not be resolved" in verdict.reason
    # A touch move activates nothing.
    assert run(evaluator(PROHIBIT_DELETE).evaluate(context_for(touch("touchMove")))).decision == Decision.ALLOW


TRIGGERED = {
    "agent_id": "agent",
    "pre_conditions": [
        {"trigger": {"click_text": ["Mark as Paid"]}, "require_page_text": {"values": ["Approved"]}},
    ],
    "post_conditions": [
        {"trigger": {"click_text": ["Mark as Paid"]}, "require_page_text": {"values": ["Reconciliation complete"]}},
    ],
}


@pytest.mark.parametrize("element", [{"text": "Mark as Paid"}, None])  # None: unresolved counts as a match
def test_click_text_triggers_match_a_touch(element: dict[str, str] | None) -> None:
    verdict = run(evaluator(TRIGGERED).evaluate(context_for(touch("touchStart"), element=element, page_text="")))
    assert verdict.decision == Decision.BLOCK
    assert verdict.rule == "require_page_text"


def test_post_conditions_run_after_a_touch_end() -> None:
    ctx = context_for(touch("touchEnd"), element={"text": "Mark as Paid"})
    ctx.post_browser_state = BrowserState(url=ctx.browser_state.url, page_text="Error")  # type: ignore[union-attr]
    verdict = run(evaluator(TRIGGERED).evaluate_post(ctx))
    assert verdict is not None
    assert verdict.decision == Decision.BLOCK
    assert verdict.rule == "require_page_text"


# Text and amount comparison --------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("山田", "田中"),  # names in a script without ASCII letters
        ("Müller", "Mller"),
        ("€5", "£5"),  # same number, different currency
        ("EUR €5,000.00", "$5,000.00"),
        ("!!!", "???"),  # nothing left after removing punctuation
        ("-5", "5"),
        ("5.000,00", "500000"),  # unparseable amounts keep their separators
        ("12/05", "1205"),
    ],
)
def test_different_values_are_not_equal(left: str, right: str) -> None:
    assert not values_equal(left, right)


def test_currency_codes_and_nonfinite_amounts_fail_comparison() -> None:
    assert not values_equal("USD 5,000", "EUR 5,000")
    assert parse_number("9" * 500) is None
    assert not values_equal("9" * 500, "8" * 500)


def test_prohibited_text_capture_limit_fails_closed() -> None:
    policy = {"agent_id": "agent", "pre_conditions": [{"prohibit_page_text": {"values": ["forbidden"]}}]}
    ctx = context_for(touch("touchStart"), page_text="safe")
    assert ctx.browser_state is not None
    ctx.browser_state.page_text_truncated = True
    verdict = run(evaluator(policy).evaluate(ctx))
    assert (verdict.decision, verdict.rule) == (Decision.BLOCK, "prohibit_page_text")


def test_remembered_field_requires_trusted_origin() -> None:
    with pytest.raises(ValidationError, match="remembered field"):
        evaluator(
            {
                "agent_id": "agent",
                "fields": [{"name": "deposit", "selector": "#d", "url_contains": "/bank", "remember": True}],
            }
        )
    fields = evaluator(
        {
            "agent_id": "agent",
            "fields": [
                {
                    "name": "deposit",
                    "selector": "#d",
                    "url_contains": "/bank",
                    "allowed_origins": ["https://bank.test"],
                    "remember": True,
                }
            ],
        }
    ).field_specs("agent")
    assert fields[0].applies("https://bank.test/bank")
    assert not fields[0].applies("https://evil.test/bank")
    absolute = evaluator(
        {
            "agent_id": "agent",
            "fields": [
                {"name": "deposit", "selector": "#d", "url_contains": "https://bank.test/bank", "remember": True}
            ],
        }
    ).field_specs("agent")
    assert absolute[0].applies("https://bank.test/bank")
    assert not absolute[0].applies("https://bank.test.evil/bank")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("山田", "山田"),
        ("Müller", "MÜLLER"),
        ("\uff21\uff22\uff23-1", "abc 1"),  # NFKC: full-width letters
        ("INV-001", "inv 001"),
        ("Stra\u00dfe", "STRASSE"),  # casefold
        ("a\u200bb", "ab"),  # an invisible format character
        ("$5,000.00", "5000"),
        ("€5", "5"),  # only one side names a currency
        ("", " "),
    ],
)
def test_equal_values_are_equal(left: str, right: str) -> None:
    assert values_equal(left, right)


def test_assert_field_equal_takes_a_number() -> None:
    policy = {"agent_id": "agent", "pre_conditions": [{"assert_field_equal": {"left": "total", "value": 5000}}]}
    ok = context_for(touch("touchMove"), extracted_fields={"total": "$5,000.00"})
    assert run(evaluator(policy).evaluate(ok)).decision == Decision.ALLOW
    bad = context_for(touch("touchMove"), extracted_fields={"total": "$5,000.01"})
    assert run(evaluator(policy).evaluate(bad)).decision == Decision.BLOCK
    float_policy = {"agent_id": "agent", "pre_conditions": [{"assert_field_equal": {"left": "total", "value": 12.5}}]}
    half = context_for(touch("touchMove"), extracted_fields={"total": "12.50"})
    assert run(evaluator(float_policy).evaluate(half)).decision == Decision.ALLOW
    with pytest.raises(ValidationError, match="not a boolean"):
        parse_rule({"assert_field_equal": {"left": "total", "value": True}}, "pre")


# URL scopes ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("https://Bank.Example:443/demo/bank?x=1#y", "https://bank.example/demo/bank"),
        ("http://127.0.0.1:8000/a", "http://127.0.0.1:8000/a"),
        ("https://user:pw@evil.example/demo/bank", "https://evil.example/demo/bank"),
        ("about:blank", "about:blank"),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("http://[::1", None),
    ],
)
def test_page_address(url: str, address: str | None) -> None:
    assert page_address(url) == address


@pytest.mark.parametrize(
    "url", ["https://evil.example/?x=/demo/bank", "https://evil.example/#/demo/bank", "https://evil.example/"]
)
def test_url_scopes_never_match_query_or_fragment(url: str) -> None:
    assert not url_in_scope("/demo/bank", url)
    assert not PolicyConfig.model_validate({"agent_id": "a", "target_url_contains": "/demo/bank"}).applies_to("a", url)
    ev = evaluator({"agent_id": "a", "fields": [{"name": "deposit", "url_contains": "/demo/bank", "selector": "#d"}]})
    assert not ev.field_specs("a")[0].applies(url)
    assert url_in_scope("/demo/bank", "https://bank.example/demo/bank?x=1")


def test_a_policy_applies_to_a_url_that_does_not_parse() -> None:
    policy = PolicyConfig.model_validate({"agent_id": "a", "target_url_contains": "/demo/bank"})
    assert policy.applies_to("a", "http://[::1")  # fail closed: its rules run


def test_scoped_out_policy_rules_do_not_run_on_a_lookalike_query() -> None:
    policy = {
        "agent_id": "agent",
        "target_url_contains": "/demo/finance",
        "pre_conditions": [{"require_page_text": {"values": ["never there"]}}],
    }
    ctx = context_for(touch("touchMove"), url="https://elsewhere.example/?next=/demo/finance")
    assert run(evaluator(policy).evaluate(ctx)).decision == Decision.ALLOW
    ctx = context_for(touch("touchMove"), url="https://portal.example/demo/finance")
    assert run(evaluator(policy).evaluate(ctx)).decision == Decision.BLOCK


# Rule options that would do nothing ------------------------------------------------------


@pytest.mark.parametrize("rule", ["require_page_text", "prohibit_page_text", "prohibit_click_text"])
@pytest.mark.parametrize("options", [None, {}, {"values": []}, {"values": [""]}])
def test_text_rules_need_values(rule: str, options: dict[str, Any] | None) -> None:
    with pytest.raises(ValidationError):
        parse_rule({rule: options}, "pre")


@pytest.mark.parametrize("rule", ["restrict_downloads", "restrict_uploads"])
def test_file_rules_take_no_trigger(rule: str) -> None:
    with pytest.raises(ValueError, match="does not take a trigger"):
        parse_rule({"trigger": {"click_text": ["Export"]}, rule: {"extensions": [".pdf"]}}, "pre")


def test_trigger_is_accepted_only_beside_the_rule() -> None:
    with pytest.raises(ValueError, match="beside the rule"):
        parse_rule({"require_page_text": {"values": ["x"], "trigger": {"click_text": ["Pay"]}}}, "pre")
    rule = parse_rule({"trigger": {"click_text": ["Pay"]}, "require_page_text": {"values": ["x"]}}, "pre")
    assert rule.trigger is not None


def test_max_downloads_fails_closed_without_a_count() -> None:
    policy = {"agent_id": "agent", "pre_conditions": [{"restrict_downloads": {"max_downloads": 5}}]}
    action = CdpAction(None, "Statelock.download", ActionKind.DOWNLOAD, {"phase": "begin", "suggested_filename": "a"})
    verdict = run(evaluator(policy).evaluate(context_for(action)))
    assert verdict.decision == Decision.BLOCK
    assert "download count is unknown" in verdict.reason


# Loading ---------------------------------------------------------------------------------


def test_policy_evaluator_takes_only_a_validated_bundle() -> None:
    policy = PolicyConfig.model_validate({"agent_id": "a"})
    for unchecked in (policy, [policy, policy]):
        with pytest.raises(TypeError, match="PolicyBundle"):
            PolicyEvaluator(unchecked)  # type: ignore[arg-type]


def test_policy_file_with_a_repeated_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        "policies:\n  - agent_id: a\n    pre_conditions:\n      - prohibit_click_text: {values: [Delete]}\n"
        "    pre_conditions: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key 'pre_conditions'"):
        load_policy_bundle(path)
