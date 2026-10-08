"""Page-text and click-text rules match however the page spells the text."""

from __future__ import annotations

from typing import Any

import pytest
from helpers import context_for, key_action, mouse_action, run
from pydantic import ValidationError

from statelock.core.enums import Decision
from statelock.core.state import BrowserState
from statelock.policy import PolicyBundle, PolicyEvaluator, parse_rule
from statelock.policy.text import compact_text, normalize_text

# The same words as a page may render them.
SPELLINGS = [
    "Mark as Paid",
    "MARK AS PAID",
    "Mark\u00a0as\u00a0Paid",  # non-breaking spaces (&nbsp;)
    "Mark\nas\n  Paid",  # line breaks
    "Mark as Pa\u00adid",  # soft hyphen
    "Mark\u200b as Paid",  # zero-width space
    "Mark as\u2060 Paid",  # word joiner
    "\uff2d\uff41\uff52\uff4b as Paid",  # full-width letters
    "Mark\u2003as\u2009Paid",  # em space, thin space
]


def evaluate(rule: dict[str, Any], **state: Any) -> Decision:
    bundle = PolicyBundle.model_validate({"policies": [{"agent_id": "agent", "pre_conditions": [rule]}]})
    return run(PolicyEvaluator(bundle).evaluate(context_for(mouse_action(), **state))).decision


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_normalize_text_treats_spellings_alike(spelling: str) -> None:
    assert normalize_text(spelling) == "mark as paid"


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_prohibit_page_text_finds_every_spelling(spelling: str) -> None:
    rule = {"prohibit_page_text": {"values": ["Mark as Paid"]}}
    assert evaluate(rule, page_text=f"Invoice 12. {spelling}. Done") == Decision.BLOCK


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_require_page_text_finds_every_spelling(spelling: str) -> None:
    rule = {"require_page_text": {"values": ["Mark as Paid"]}}
    assert evaluate(rule, page_text=f"Invoice 12. {spelling}. Done") == Decision.ALLOW


# "Mark as<b>Paid</b>" renders without a space; letter-spaced text has extra ones.
@pytest.mark.parametrize("page", ["Mark asPaid", "MarkasPaid", "M a r k  a s  P a i d"])
def test_prohibit_page_text_matches_across_spaces(page: str) -> None:
    assert evaluate({"prohibit_page_text": {"values": ["Mark as Paid"]}}, page_text=page) == Decision.BLOCK


def test_require_page_text_still_needs_the_words() -> None:
    rule = {"require_page_text": {"values": ["Reconciliation complete"]}}
    assert evaluate(rule, page_text="Reconciliation incomplete") == Decision.BLOCK
    assert evaluate(rule, page_text="Reconciliation pending") == Decision.BLOCK


def test_policy_values_are_normalized_too() -> None:
    rule = {"prohibit_page_text": {"values": ["Mark\u00a0as\u00a0PAID"]}}
    assert evaluate(rule, page_text="mark as paid") == Decision.BLOCK


@pytest.mark.parametrize("value", [" ", "\u00ad", "\u200b\u200b", "\n"])
def test_text_values_need_visible_characters(value: str) -> None:
    for rule in ("prohibit_page_text", "require_page_text", "prohibit_click_text"):
        with pytest.raises(ValidationError, match="visible characters"):
            parse_rule({rule: {"values": [value]}}, "pre")


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_prohibit_click_text_uses_the_same_normalization(spelling: str) -> None:
    rule = {"prohibit_click_text": {"values": ["Mark as Paid"]}}
    assert evaluate(rule, element={"tag_name": "BUTTON", "text": spelling}) == Decision.BLOCK


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_click_text_trigger_uses_the_same_normalization(spelling: str) -> None:
    bundle = PolicyBundle.model_validate(
        {
            "policies": [
                {
                    "agent_id": "agent",
                    "pre_conditions": [
                        {"trigger": {"click_text": ["Mark as Paid"]}, "require_page_text": {"values": ["approved"]}}
                    ],
                }
            ]
        }
    )
    ctx = context_for(key_action(), element={"tag_name": "BUTTON", "text": spelling}, page_text="pending")
    assert run(PolicyEvaluator(bundle).evaluate(ctx)).decision == Decision.BLOCK


def test_prohibit_page_text_blocks_when_parts_of_the_page_were_not_read() -> None:
    rule = {"prohibit_page_text": {"values": ["Mark as Paid"]}}
    unread = BrowserState(url="https://x.test/a", page_text="safe", page_text_unread=["https://frame.test/"])
    assert evaluate(rule, state=unread) == Decision.BLOCK
    whole = BrowserState(url="https://x.test/a", page_text="safe")
    assert evaluate(rule, state=whole) == Decision.ALLOW


def test_require_page_text_ignores_unread_parts() -> None:
    rule = {"require_page_text": {"values": ["safe"]}}
    state = BrowserState(url="https://x.test/a", page_text="safe", page_text_unread=["https://frame.test/"])
    assert evaluate(rule, state=state) == Decision.ALLOW


def test_compact_text_drops_all_whitespace() -> None:
    assert compact_text(" Mark\u00a0as \n Paid ") == "markaspaid"
