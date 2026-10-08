from pathlib import Path
from typing import ClassVar

import pytest
from helpers import context_for, mouse_action, run
from pydantic import ValidationError

from statelock.core.actions import ActionKind, CdpAction
from statelock.core.enums import Decision
from statelock.core.state import BrowserState
from statelock.policy import PolicyBundle, PolicyEvaluator, load_policy_bundle
from statelock.policy.fields import FieldSpec, ScopedField, SessionMemory, parse_number, resolve_fields
from statelock.policy.rules import RULES, Rule, RuleContext, RuleFailure

ROOT = Path(__file__).resolve().parents[1]


def _evaluator(*policies: dict) -> PolicyEvaluator:
    return PolicyEvaluator(PolicyBundle.model_validate({"policies": list(policies)}))


# Field specs ------------------------------------------------------------------------------


def test_field_spec_validation() -> None:
    FieldSpec(name="amount", selector="#a", pattern=r"\$([0-9,.]+)")
    for bad in (
        {"name": "a"},
        {"name": "a", "selector": "#a", "key": "k"},
        {"name": "a", "key": "k", "attribute": "title"},
        {"name": "a", "selector": "#a", "pattern": "("},
        {"name": "a", "selector": "#a", "pattern": "(a)(b)"},
        {"name": "remembered.x", "selector": "#a"},
        {"name": "a", "selector": "#a", "unknown": 1},
    ):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate(bad)


def test_field_names_must_be_consistent_per_agent() -> None:
    with pytest.raises(ValidationError, match="defined twice"):
        PolicyBundle.model_validate(
            {
                "policies": [
                    {"agent_id": "a", "fields": [{"name": "x", "selector": "#one"}]},
                    {"agent_id": "a", "fields": [{"name": "x", "selector": "#two"}]},
                ]
            }
        )


def test_field_scope_defaults_to_policy_target() -> None:
    ev = _evaluator(
        {
            "agent_id": "a",
            "target_url_contains": "/erp",
            "fields": [
                {"name": "invoice", "selector": "#i"},
                {
                    "name": "deposit",
                    "selector": "#d",
                    "url_contains": "/bank",
                    "allowed_origins": ["https://bank.test"],
                    "remember": True,
                },
            ],
        },
        {"agent_id": "b", "fields": [{"name": "other", "key": "k"}]},
    )
    scopes = {f.spec.name: f.url_contains for f in ev.field_specs("a")}
    assert scopes == {"invoice": "/erp", "deposit": "/bank"}
    assert [f.url_contains for f in ev.field_specs("b")] == [None]


def test_resolve_fields_applies_scope_key_and_pattern() -> None:
    fields = [
        ScopedField(FieldSpec(name="deposit", selector="#d", pattern=r"\$([0-9,.]+)"), "/bank"),
        ScopedField(FieldSpec(name="alias", key="erp_invoice_amount"), None),
        ScopedField(FieldSpec(name="nomatch", selector="#n", pattern=r"EUR"), None),
    ]
    values = resolve_fields(
        fields,
        "https://portal.test/bank/1",
        {"erp_invoice_amount": "$5.00"},
        {"deposit": "Amount: $4,900.00 USD", "nomatch": "$1"},
    )
    assert values == {"erp_invoice_amount": "$5.00", "alias": "$5.00", "deposit": "4,900.00"}
    assert "deposit" not in resolve_fields(fields, "https://portal.test/erp", {}, {"deposit": "$1"})


def test_policy_fields_are_never_taken_from_page_markup() -> None:
    fields = [
        ScopedField(FieldSpec(name="erp_invoice", selector="#invoice"), "/erp"),
        ScopedField(FieldSpec(name="deposit", key="bank_amount"), None),
    ]
    markup = {"erp_invoice": "$1.00", "deposit": "$2.00", "bank_amount": "$3.00", "note": "x"}
    # Out of scope, or the selector found nothing: the field is missing, not the page's value.
    assert resolve_fields(fields, "https://portal.test/bank", markup, {}) == {
        "bank_amount": "$3.00",
        "note": "x",
        "deposit": "$3.00",
    }
    assert "erp_invoice" not in resolve_fields(fields, "https://portal.test/erp", markup, {})
    assert resolve_fields(fields, "https://portal.test/erp", markup, {"erp_invoice": "$9.00"})["erp_invoice"] == "$9.00"


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("$5,000.00", 5000.0),
        ("USD 1200", 1200.0),
        ("(12.50)", -12.5),
        ("(1,234.00)", -1234.0),
        ("($1,234.00)", -1234.0),
        ("-3", -3.0),
        ("-$5,000", -5000.0),
        ("$-5,000", -5000.0),
        ("-USD 5", -5.0),
        ("\u22125", -5.0),  # MINUS SIGN
        ("Balance: -$500.25", -500.25),
        ("+5", 5.0),
        ("5,000.00 USD", 5000.0),
        ("\uffe5\uff11,\uff12\uff10\uff10", 1200.0),  # full-width
        ("Re-issued $5", 5.0),
        (7, 7.0),
    ],
)
def test_parse_number(text: object, number: float) -> None:
    assert parse_number(text) == number


@pytest.mark.parametrize(
    "text",
    [
        "5.000,00",
        "1,23",
        "abc",
        "",
        "1-2",
        True,
        float("nan"),
        "5-",  # trailing sign
        "12.50)",  # unmatched parenthesis
        "(12.50",
        "-(5)",
        "(-5)",
        "--5",
        "-$-5",
        "- 5",  # a sign apart from the number
        "\u20135",  # EN DASH, not a minus
        "INV-5",  # a hyphen
        "Fee ($5)",  # parentheses around part of the text
        ".5",
    ],
)
def test_parse_number_rejects_ambiguous_values(text: object) -> None:
    assert parse_number(text) is None


# Session memory -----------------------------------------------------------------------------


def test_session_memory_keeps_latest_matching_value() -> None:
    memory = SessionMemory(
        [
            ScopedField(FieldSpec(name="deposit", selector="#d", remember=True), "/bank"),
            ScopedField(FieldSpec(name="invoice", selector="#i"), "/erp"),
        ]
    )
    assert memory.enabled
    assert memory.update(BrowserState(url="https://x/bank", extracted_fields={"deposit": "1"}), "page_load") == [
        "deposit"
    ]
    memory.update(BrowserState(url="https://x/bank", extracted_fields={"deposit": "2"}), "action", 4)
    memory.update(BrowserState(url="https://x/erp", extracted_fields={"deposit": "9", "invoice": "3"}), "action")
    memory.update(BrowserState(url="https://x/bank", capture_error="boom", extracted_fields={"deposit": "8"}), "x")
    snapshot = memory.snapshot()
    assert list(snapshot) == ["deposit"]
    assert snapshot["deposit"]["value"] == "2"
    assert snapshot["deposit"]["sequence"] == 4
    assert snapshot["deposit"]["url"] == "https://x/bank"
    assert not SessionMemory([ScopedField(FieldSpec(name="i", selector="#i"), None)]).enabled


# Rules ----------------------------------------------------------------------------------------


DEPOSIT_FIELD = {"name": "deposit", "selector": "#d", "url_contains": "https://x/bank", "remember": True}


def _remembered(value: str) -> dict:
    return {"deposit": {"value": value, "url": "https://x/bank", "source": "page_load"}}


def test_assert_field_equal_with_remembered_value() -> None:
    ev = _evaluator(
        {
            "agent_id": "agent",
            "fields": [DEPOSIT_FIELD],
            "pre_conditions": [{"assert_field_equal": {"left": "remembered.deposit", "right": "inv"}}],
        }
    )
    ctx = context_for(mouse_action(), element={"text": "x"}, extracted_fields={"inv": "$5,000.00"})
    missing = run(ev.evaluate(ctx))
    assert missing.decision == Decision.BLOCK
    assert "remembered.deposit" in missing.reason
    ctx.remembered = _remembered("$4,900.00")
    blocked = run(ev.evaluate(ctx))
    assert blocked.decision == Decision.BLOCK
    assert blocked.evidence["remembered"]["remembered.deposit"]["url"] == "https://x/bank"
    ctx.remembered = _remembered("5000.00")
    assert run(ev.evaluate(ctx)).decision == Decision.ALLOW


@pytest.mark.parametrize(
    ("rule", "fields", "decision"),
    [
        ({"left": "inv", "op": "<=", "value": 10000}, {"inv": "$5,000.00"}, Decision.ALLOW),
        ({"left": "inv", "op": "<=", "value": 10000}, {"inv": "$25,000.00"}, Decision.BLOCK),
        ({"left": "inv", "op": ">", "value": 0}, {"inv": "(10.00)"}, Decision.BLOCK),
        ({"left": "inv", "op": "<=", "value": 10000}, {"inv": "n/a"}, Decision.BLOCK),
        ({"left": "inv", "op": "<=", "value": 10000}, {}, Decision.BLOCK),
        ({"left": "remembered.deposit", "op": ">=", "right": "inv"}, {"inv": "$5,000.00"}, Decision.ALLOW),
        ({"left": "remembered.deposit", "op": "==", "right": "inv"}, {"inv": "$5,000.01"}, Decision.BLOCK),
        ({"left": "remembered.deposit", "op": "!=", "right": "inv"}, {"inv": "1"}, Decision.ALLOW),
    ],
)
def test_assert_compare(rule: dict, fields: dict, decision: Decision) -> None:
    ev = _evaluator({"agent_id": "agent", "fields": [DEPOSIT_FIELD], "pre_conditions": [{"assert_compare": rule}]})
    ctx = context_for(mouse_action(), element={"text": "x"}, extracted_fields=fields)
    ctx.remembered = _remembered("$5,000.00")
    assert run(ev.evaluate(ctx)).decision == decision


def test_assert_compare_requires_one_right_side() -> None:
    for rule in (
        {"left": "a", "op": "<"},
        {"left": "a", "op": "<", "right": "b", "value": 1},
        {"left": "a", "op": "~", "value": 1},
    ):
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate({"policies": [{"agent_id": "a", "pre_conditions": [{"assert_compare": rule}]}]})


def _upload_context(uploads: object):
    params = {"files": ["/u/a"], "nodeId": 3}
    if uploads is not None:
        params["statelock_uploads"] = uploads
    action = CdpAction(9, "DOM.setFileInputFiles", ActionKind.FILE_UPLOAD, params, "S1")
    return context_for(action)


def test_restrict_uploads() -> None:
    ev = _evaluator(
        {
            "agent_id": "agent",
            "pre_conditions": [
                {
                    "restrict_uploads": {
                        "extensions": ["pdf", ".CSV"],
                        "max_bytes": 100,
                        "max_files": 1,
                        "name_pattern": "^INV",
                    }
                }
            ],
        }
    )

    def verdict(*files: dict) -> Decision:
        return run(ev.evaluate(_upload_context(list(files)))).decision

    assert verdict({"name": "INV-1.pdf", "size": 10}) == Decision.ALLOW
    assert verdict({"name": "INV-1.Csv", "size": 100}) == Decision.ALLOW
    assert verdict({"name": "INV-1.exe", "size": 10}) == Decision.BLOCK
    assert verdict({"name": "INV-1.pdf", "size": 101}) == Decision.BLOCK
    assert verdict({"name": "report.pdf", "size": 10}) == Decision.BLOCK
    assert verdict({"name": "INV-1.pdf", "size": 1}, {"name": "INV-2.pdf", "size": 1}) == Decision.BLOCK
    assert run(ev.evaluate(_upload_context(None))).decision == Decision.BLOCK
    # Other actions are not affected.
    assert run(ev.evaluate(context_for(mouse_action(), element={"text": "x"}))).decision == Decision.ALLOW


def test_restrict_uploads_is_pre_only() -> None:
    with pytest.raises(ValidationError):
        PolicyBundle.model_validate(
            {"policies": [{"agent_id": "a", "post_conditions": [{"restrict_uploads": {"max_files": 1}}]}]}
        )


def test_default_policy_file_loads() -> None:
    bundle = load_policy_bundle(ROOT / "policies" / "default.yaml")
    assert "portal_reconciliation_agent" in {p.agent_id for p in bundle.policies}


@pytest.mark.parametrize("text", ["Qty 2 x $500", "Invoice #12 $5,000.00", "Due 12/05 $500", "10 000"])
def test_parse_number_needs_exactly_one_number(text: str) -> None:
    assert parse_number(text) is None


def test_assert_field_equal_compares_numbers_numerically() -> None:
    from statelock.policy.rules import values_equal

    assert values_equal("5000", "$5,000.00")
    assert values_equal("INV-1042", "inv-1042")
    assert not values_equal("5000", "$5,000.01")


# remembered.<name> references --------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        {"assert_field_equal": {"left": "remembered.typo", "right": "inv"}},
        {"assert_compare": {"left": "inv", "op": "<=", "right": "remembered.typo"}},
        {
            "visual_assert": {
                "check_name": "c",
                "instruction": "i",
                "extract": {"amount": "string"},
                "cross_check": [{"extracted": "amount", "field": "remembered.typo"}],
            }
        },
    ],
)
def test_unknown_remembered_reference_is_rejected_at_load(rule: dict) -> None:
    policy = {"agent_id": "agent", "fields": [DEPOSIT_FIELD], "pre_conditions": [rule]}
    with pytest.raises(ValidationError, match=r"remembered\.typo"):
        PolicyBundle.model_validate({"policies": [policy]})


def test_remembered_reference_needs_a_remember_field_of_the_same_agent() -> None:
    rule = {"assert_field_equal": {"left": "remembered.deposit", "right": "inv"}}
    not_remembered = {**DEPOSIT_FIELD, "remember": False}
    with pytest.raises(ValidationError, match=r"remembered\.deposit"):
        PolicyBundle.model_validate(
            {"policies": [{"agent_id": "agent", "fields": [not_remembered], "pre_conditions": [rule]}]}
        )
    with pytest.raises(ValidationError, match=r"remembered\.deposit"):
        PolicyBundle.model_validate(
            {
                "policies": [
                    {"agent_id": "other", "fields": [DEPOSIT_FIELD]},
                    {"agent_id": "agent", "pre_conditions": [rule]},
                ]
            }
        )
    # Another policy of the same agent may define it.
    PolicyBundle.model_validate(
        {
            "policies": [
                {"agent_id": "agent", "target_url_contains": "/bank", "fields": [DEPOSIT_FIELD]},
                {"agent_id": "agent", "target_url_contains": "/erp", "post_conditions": [rule]},
            ]
        }
    )


class ReadsRemembered(Rule):
    rule_name: ClassVar[str] = "reads_remembered_test"
    invoice_field: str

    async def check(self, ctx: RuleContext) -> RuleFailure | None:  # noqa: ARG002
        return None


def test_custom_rule_remembered_references_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(RULES, ReadsRemembered.rule_name, ReadsRemembered)
    rule = {"reads_remembered_test": {"invoice_field": "remembered.typo"}}
    with pytest.raises(ValidationError, match=r"remembered\.typo"):
        PolicyBundle.model_validate({"policies": [{"agent_id": "agent", "pre_conditions": [rule]}]})


@pytest.mark.parametrize(("pattern", "message"), [("(", "not a valid regex"), ("(a)(b)", "at most one group")])
def test_field_pattern_is_checked_when_the_policy_loads(pattern: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        FieldSpec.model_validate({"name": "total", "selector": "#t", "pattern": pattern})
