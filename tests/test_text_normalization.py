"""One text normaliser and one click-label matcher for every text rule and trigger."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from statelock.core.state import TargetElement
from statelock.policy.fields import parse_number
from statelock.policy.rules import values_equal
from statelock.policy.text import comparable_text, label_matches, normalize_text
from statelock.policy.triggers import ActionTrigger


def test_normalize_text_can_keep_case() -> None:
    assert normalize_text("\uff35\uff33\uff24\u00a05\u200b") == "usd 5"
    assert normalize_text("\uff35\uff33\uff24\u00a05\u200b", casefold=False) == "USD 5"


@pytest.mark.parametrize(
    ("value", "comparable"),
    [
        ("INV-001", "inv001"),
        ("12/05", "12/05"),
        ("1 / 5", "1/5"),  # spaces go first, so the slash is between digits
        ("- 5", "-5"),  # the sign is kept once the space is gone
        ("1­.5", "1.5"),  # an invisible character does not hide the separator
        ("Straße!", "strasse"),
    ],
)
def test_comparable_text(value: str, comparable: str) -> None:
    assert comparable_text(value) == comparable


@pytest.mark.parametrize(("left", "right"), [("1 / 5", "15"), ("- 5", "5"), ("12 / 05", "1205")])
def test_separators_beside_spaces_still_count(left: str, right: str) -> None:
    assert not values_equal(left, right)


def test_amounts_ignore_invisible_characters() -> None:
    assert parse_number("$5\u200b000") == 5000
    assert parse_number("\u00a0USD\u00a05\u00a0") == 5


@pytest.mark.parametrize(
    ("element", "matches"),
    [
        ({"tag_name": "BUTTON", "text": "Mark as Paid"}, True),
        ({"tag_name": "BUTTON", "text": "Cancel"}, False),
        ({"tag_name": "DIV", "aria_label": "MARK AS PAID"}, True),
        ({"tag_name": "BUTTON"}, True),  # interactive without a label: any text might be on it
        ({"tag_name": "DIV"}, False),  # not interactive, no label
        ({"tag_name": "IFRAME", "unresolved": True, "text": "Cancel"}, True),  # a frame Statelock cannot read
    ],
)
def test_label_matches(element: dict[str, object], *, matches: bool) -> None:
    assert label_matches(TargetElement.model_validate(element), ["Mark as Paid"]) is matches


@pytest.mark.parametrize("value", ["", " ", "\u200b"])
def test_trigger_click_text_needs_visible_characters(value: str) -> None:
    with pytest.raises(ValidationError, match="visible characters"):
        ActionTrigger.model_validate({"click_text": [value]})
