from pathlib import Path

import pytest
from helpers import URL, context_for, key_action, mouse_action, run
from pydantic import ValidationError

from statelock.core.enums import Decision, SystemRule, ViolationType
from statelock.core.state import BrowserState, TargetElement
from statelock.policy import (
    ActionTrigger,
    NotConfiguredPerceptionEvaluator,
    PerceptionRequest,
    PolicyBundle,
    PolicyConfig,
    PolicyEvaluator,
    load_policy_bundle,
    parse_rule,
)


def evaluator(*policies: dict) -> PolicyEvaluator:
    return PolicyEvaluator(PolicyBundle.model_validate({"policies": list(policies)}))


FINANCE = {
    "agent_id": "agent",
    "target_url_contains": "/demo/finance",
    "pre_conditions": [
        {"assert_field_equal": {"left": "bank", "right": "erp"}},
        {"prohibit_click_text": {"values": ["Delete"]}},
    ],
    "post_conditions": [
        {"trigger": {"click_text": ["Mark as Paid"]}, "require_page_text": {"values": ["Reconciliation complete"]}},
        {"trigger": {"key": ["Enter"]}, "require_page_text": {"values": ["Saved"]}},
    ],
}


# Rule parsing ---------------------------------------------------------------------------


def test_parse_rule_requires_exactly_one_known_rule() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse_rule({}, "pre")
    with pytest.raises(ValueError, match="unknown"):
        parse_rule({"no_such_rule": {}}, "pre")
    with pytest.raises(ValueError, match="exactly one"):
        parse_rule({"require_page_text": {"values": []}, "prohibit_click_text": {"values": []}}, "pre")


def test_parse_rule_checks_phase() -> None:
    with pytest.raises(ValueError, match="not allowed in post"):
        parse_rule({"prohibit_click_text": {"values": ["x"]}}, "post")


def test_policy_config_requires_agent_id_and_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate({"target_url_contains": "/x"})
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate({"agent_id": "a", "typo_field": 1})


def test_trigger_requires_click_text_or_key() -> None:
    with pytest.raises(ValidationError):
        ActionTrigger.model_validate({})
    assert ActionTrigger.model_validate({"key": ["Enter"]}).key == ["Enter"]


def test_load_policy_bundle(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policy_bundle(tmp_path / "missing.yaml")
    single = tmp_path / "single.yaml"
    single.write_text("agent_id: solo\npre_conditions:\n  - require_page_text: {values: [Hi]}\n", encoding="utf-8")
    bundle = load_policy_bundle(single)
    assert bundle.policies[0].agent_id == "solo"
    assert bundle.policies[0].pre_conditions[0].rule_name == "require_page_text"


# Gates -----------------------------------------------------------------------------------


def test_unregistered_agent_is_blocked() -> None:
    verdict = run(evaluator(FINANCE).evaluate(context_for(mouse_action(), agent_id="other", element={"text": "x"})))
    assert verdict.decision == Decision.BLOCK
    assert verdict.rule == SystemRule.AGENT_REGISTRATION.value


def test_capture_error_and_missing_state_fail_closed() -> None:
    failed = context_for(mouse_action(), state=BrowserState(capture_error="timeout"))
    assert run(evaluator(FINANCE).evaluate(failed)).rule == SystemRule.STATE_CAPTURE.value
    missing = context_for(mouse_action())
    missing.browser_state = None
    assert run(evaluator(FINANCE).evaluate(missing)).rule == SystemRule.STATE_CAPTURE.value


def test_policy_for_other_agent_or_url_does_not_apply() -> None:
    policies = [FINANCE, {"agent_id": "other", "pre_conditions": [{"require_page_text": {"values": ["never"]}}]}]
    ctx = context_for(mouse_action(), url="https://elsewhere.test", element={"text": "x"})
    assert run(evaluator(*policies).evaluate(ctx)).decision == Decision.ALLOW


# Policy ids ----------------------------------------------------------------------------


def test_policies_have_ids_and_verdicts_name_the_one_that_blocked() -> None:
    bundle = PolicyBundle.model_validate(
        {
            "policies": [
                {"agent_id": "agent", "target_url_contains": "/bank"},
                {
                    "id": "erp-invoice-check",
                    "agent_id": "agent",
                    "pre_conditions": [{"prohibit_click_text": {"values": ["Delete"]}}],
                },
                {"agent_id": "agent", "pre_conditions": [{"require_page_text": {"values": ["Invoice"]}}]},
                {"agent_id": "other"},
            ]
        }
    )
    assert [p.policy_id for p in bundle.policies] == ["agent#1", "erp-invoice-check", "agent#3", "other#1"]
    ev = PolicyEvaluator(bundle)
    blocked = run(ev.evaluate(context_for(mouse_action(), element={"text": "Delete"}, page_text="Invoice")))
    assert (blocked.policy_id, blocked.rule) == ("erp-invoice-check", "prohibit_click_text")
    no_text = run(ev.evaluate(context_for(mouse_action(), element={"text": "Keep"}, page_text="")))
    assert (no_text.policy_id, no_text.rule) == ("agent#3", "require_page_text")


def test_policy_ids_are_unique() -> None:
    with pytest.raises(ValidationError, match="used by more than one policy"):
        PolicyBundle.model_validate({"policies": [{"id": "p", "agent_id": "a"}, {"id": "p", "agent_id": "b"}]})
    with pytest.raises(ValidationError, match="used by more than one policy"):  # an id may not take a default's name
        PolicyBundle.model_validate({"policies": [{"agent_id": "a"}, {"id": "a#1", "agent_id": "b"}]})
    with pytest.raises(ValidationError):
        PolicyBundle.model_validate({"policies": [{"id": "has spaces", "agent_id": "a"}]})


# Pre-condition rules ---------------------------------------------------------------------


def test_assert_field_equal() -> None:
    ev = evaluator(FINANCE)
    mismatch = context_for(mouse_action(), element={"text": "x"}, extracted_fields={"bank": "$4,900", "erp": "$5,000"})
    verdict = run(ev.evaluate(mismatch))
    assert verdict.rule == "assert_field_equal"
    assert verdict.policy_id == "agent#1"  # the agent's first policy (no id: in the file)
    match = context_for(
        mouse_action(), element={"text": "x"}, extracted_fields={"bank": "$5,000.00", "erp": "5000.00$"}
    )
    assert run(ev.evaluate(match)).decision == Decision.ALLOW


def _prohibit_only() -> PolicyEvaluator:
    return evaluator({"agent_id": "agent", "pre_conditions": [{"prohibit_click_text": {"values": ["Delete"]}}]})


def test_prohibit_click_text_pointer() -> None:
    ev = _prohibit_only()
    assert run(ev.evaluate(context_for(mouse_action(), element={"text": "Delete invoice"}))).decision == Decision.BLOCK
    assert run(ev.evaluate(context_for(mouse_action(), element={"aria_label": "delete"}))).decision == Decision.BLOCK
    assert run(ev.evaluate(context_for(mouse_action(), element={"tag_name": "DIV"}))).decision == Decision.ALLOW
    unresolved = run(ev.evaluate(context_for(mouse_action())))
    assert unresolved.decision == Decision.BLOCK
    assert "could not be resolved" in unresolved.reason
    # Moving over, scrolling past or releasing on it does not activate it.
    for event in ("mouseMoved", "mouseWheel", "mouseReleased"):
        hover = context_for(mouse_action(event), element={"text": "Delete invoice"})
        assert run(ev.evaluate(hover)).decision == Decision.ALLOW


def test_prohibit_click_text_tap_and_drop() -> None:
    from statelock.core.actions import ActionKind, CdpAction

    ev = _prohibit_only()
    tap = CdpAction(3, "Input.synthesizeTapGesture", ActionKind.TOUCH, {"x": 1, "y": 2}, "S1")
    drop = CdpAction(4, "Input.dispatchDragEvent", ActionKind.DRAG, {"type": "drop", "x": 1, "y": 2}, "S1")
    for action in (tap, drop):
        assert run(ev.evaluate(context_for(action, element={"text": "Delete"}))).decision == Decision.BLOCK
        assert run(ev.evaluate(context_for(action, element={"text": "Keep"}))).decision == Decision.ALLOW
    scroll = CdpAction(5, "Input.synthesizeScrollGesture", ActionKind.TOUCH, {"x": 1, "y": 2}, "S1")
    assert run(ev.evaluate(context_for(scroll, element={"text": "Delete"}))).decision == Decision.ALLOW


def test_prohibit_click_text_keys() -> None:
    ev = _prohibit_only()
    enter = context_for(key_action("keyDown"), element={"source": "focus", "text": "Delete"})
    assert run(ev.evaluate(enter)).decision == Decision.BLOCK
    typing = context_for(key_action("keyDown", key="a", code="KeyA"), element={"text": "Delete"})
    assert run(ev.evaluate(typing)).decision == Decision.ALLOW
    no_focus = context_for(key_action("keyDown"))
    assert run(ev.evaluate(no_focus)).decision == Decision.ALLOW


def test_click_text_normalizes_visual_label_and_blocks_empty_button() -> None:
    ev = _prohibit_only()
    for label in ("Delete\u00a0invoice", "Delete\n invoice", "Dele\u00adte invoice"):
        ctx = context_for(mouse_action("mousePressed"), element={"tag_name": "BUTTON", "text": label})
        assert run(ev.evaluate(ctx)).decision == Decision.BLOCK
    empty = context_for(mouse_action("mousePressed"), element={"tag_name": "BUTTON", "text": ""})
    assert run(ev.evaluate(empty)).decision == Decision.BLOCK


def test_visual_assert_fails_closed_without_perception() -> None:
    ev = evaluator(
        {"agent_id": "agent", "pre_conditions": [{"visual_assert": {"check_name": "c", "instruction": "i"}}]}
    )
    verdict = run(ev.evaluate(context_for(mouse_action(), element={"text": "x"})))
    assert verdict.rule == "visual_assert"
    assert verdict.evidence["perception"]["passed"] is False


def test_not_configured_perception_evaluator_fails_closed() -> None:
    request = PerceptionRequest(check_name="c", instruction="i", context=context_for(mouse_action()))
    assert run(NotConfiguredPerceptionEvaluator().evaluate(request)).passed is False


# Post-conditions ---------------------------------------------------------------------------


def _post(action, element, page_text, post_url=URL):
    ctx = context_for(action, element=element)
    ctx.post_browser_state = BrowserState(url=post_url, page_text=page_text)
    return run(evaluator(FINANCE).evaluate_post(ctx))


def test_click_text_trigger() -> None:
    assert _post(mouse_action("mouseReleased"), {"text": "Bank"}, "nothing").decision == Decision.ALLOW
    blocked = _post(mouse_action("mouseReleased"), {"text": "Mark as Paid"}, "nothing")
    assert blocked.decision == Decision.BLOCK
    assert blocked.violation_type == ViolationType.POST_CONDITION
    assert (
        _post(mouse_action("mouseReleased"), {"text": "Mark as Paid"}, "Reconciliation complete").decision
        == Decision.ALLOW
    )


def test_unresolved_click_target_counts_as_trigger() -> None:
    assert _post(mouse_action("mouseReleased"), None, "nothing").decision == Decision.BLOCK


def test_post_policy_uses_pre_action_url() -> None:
    verdict = _post(mouse_action("mouseReleased"), {"text": "Mark as Paid"}, "nothing", post_url="http://x/elsewhere")
    assert verdict.decision == Decision.BLOCK


def test_key_trigger_and_focused_click_text() -> None:
    # Enter with nothing focused: only the key-triggered condition runs.
    only_key = _post(key_action("keyUp"), None, "Reconciliation complete")
    assert only_key.decision == Decision.BLOCK
    assert only_key.reason.endswith("Saved")
    # Enter on a focused "Mark as Paid" runs the click_text condition too.
    focused = _post(key_action("keyUp"), {"source": "focus", "text": "Mark as Paid"}, "Saved")
    assert focused.decision == Decision.BLOCK
    assert "Reconciliation complete" in focused.reason


def test_post_capture_error_fails_closed() -> None:
    ctx = context_for(mouse_action("mouseReleased"), element={"text": "x"})
    ctx.post_browser_state = BrowserState(capture_error="timeout")
    verdict = run(evaluator(FINANCE).evaluate_post(ctx))
    assert verdict.rule == SystemRule.STATE_CAPTURE.value
    assert verdict.violation_type == ViolationType.POST_CONDITION


def test_post_evaluated_evidence() -> None:
    verdict = _post(mouse_action("mouseReleased"), {"text": "Bank"}, "nothing")
    assert verdict.evidence["post_conditions_evaluated"] == [
        {"policy_id": "agent#1", "index": 0, "triggered": False},
        {"policy_id": "agent#1", "index": 1, "triggered": False},
    ]


# Guard scope -------------------------------------------------------------------------------


def test_guard_url_patterns() -> None:
    ev = evaluator(
        {"agent_id": "a", "target_url_contains": "/one"},
        {"agent_id": "a", "target_url_contains": "/two"},
        {"agent_id": "a", "target_url_contains": "/open", "allow_synthetic_events": True},
        {"agent_id": "b"},
        {"agent_id": "c", "allow_synthetic_events": True},
    )
    assert ev.guard_url_patterns("a") == ["/one", "/two"]
    assert ev.guard_url_patterns("b") is None
    assert ev.guard_url_patterns("c") == []


def test_target_element_label_text() -> None:
    assert TargetElement(text=" Pay ", aria_label="Pay invoice").label_text == "Pay Pay invoice"
    assert TargetElement().label_text == ""
