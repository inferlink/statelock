"""Amounts compare exactly, and never across currencies."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from statelock.policy import parse_rule
from statelock.policy.currency import currencies
from statelock.policy.fields import apply_pattern, parse_number
from statelock.policy.rules import values_equal


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("$5,000.00", "5,000.00 EUR"),  # a symbol against a code
        ("$5,000.00", "USD 5,000.00"),  # "$" is not only USD
        ("¥5000", "JPY 5000"),  # "¥" is JPY or CNY
        ("SEK 5", "NOK 5"),  # codes beyond the common ones
        ("5 sek", "5 nok"),  # lower case is not a code, but makes the amount unclear
        ("£5", "gbp 5"),
        ("PLN 10", "CZK 10"),
        ("€5", "5 GBP"),
        ("₹100", "100 PKR"),
        ("US$5", "C$5"),
        ("A$5", "$5"),
        ("CN¥5", "JP¥5"),
        ("₿1", "$1"),  # a symbol without a code is its own currency
    ],
)
def test_different_currencies_are_never_equal(left: str, right: str) -> None:
    assert not values_equal(left, right)
    assert not values_equal(right, left)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("€5,000.00", "5000 EUR"),
        ("₩1000", "KRW 1,000"),
        ("US$5", "USD 5.00"),
        ("C$5", "5 CAD"),
        ("$5", "$5.00"),
        ("¥500", "¥ 500"),
        ("$5,000.00", "5000"),  # a bare number names no currency
    ],
)
def test_same_currency_compares_as_numbers(left: str, right: str) -> None:
    assert values_equal(left, right)


def test_currencies_maps_symbols_and_codes() -> None:
    assert currencies("US$5") == {"USD"}
    assert currencies("$5") == {"$"}
    assert currencies("€5 / 5 EUR") == {"EUR"}
    assert currencies("5 XYZ") == set()  # not an ISO 4217 code
    assert currencies("5") == set()


def test_large_amounts_compare_exactly() -> None:
    assert parse_number("9007199254740993") == Decimal("9007199254740993")
    assert not values_equal("9007199254740993", "9007199254740992")
    assert not values_equal("$90,071,992,547,409,930.01", "$90,071,992,547,409,930.02")
    assert parse_number("-" + "1" * 40) == -int("1" * 40)
    assert parse_number(0.1) == Decimal("0.1")


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_numbers_do_not_parse(value: object) -> None:
    assert parse_number(value) is None


@pytest.mark.parametrize("value", [True, False, float("inf"), "inf", "nan"])
def test_assert_compare_value_must_be_a_finite_number(value: object) -> None:
    with pytest.raises(ValidationError):
        parse_rule({"assert_compare": {"left": "a", "op": "<=", "value": value}}, "pre")


def test_assert_compare_value_is_exact() -> None:
    rule = parse_rule({"assert_compare": {"left": "a", "op": "<=", "value": 9007199254740993}}, "pre")
    assert rule.value == Decimal("9007199254740993")  # type: ignore[attr-defined]


def test_assert_field_equal_number_constant_is_written_out() -> None:
    rule = parse_rule({"assert_field_equal": {"left": "a", "value": 1e20}}, "pre")
    assert rule.value == "100000000000000000000"  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        parse_rule({"assert_field_equal": {"left": "a", "value": float("nan")}}, "pre")


def test_optional_pattern_group_that_did_not_match_is_missing() -> None:
    assert apply_pattern("Total: n/a", r"Total:\s*(\d+)?") is None
    assert apply_pattern("Total: 12", r"Total:\s*(\d+)?") == "12"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("5 EUR", "Top up: 5 EUR"),  # "Top" is not TOP (Tongan pa\u02bbanga)
        ("ALL items 5 EUR", "5 EUR"),  # ALL is not next to the number
        ("all of 5 EUR", "€5"),
        ("(5.00 EUR)", "-5 EUR"),
        ("USD -5", "-USD 5"),
        ("USD5", "5.00 USD"),
    ],
)
def test_only_codes_next_to_the_number_name_a_currency(left: str, right: str) -> None:
    assert values_equal(left, right)
    assert values_equal(right, left)


def test_currency_codes_are_upper_case_and_next_to_the_number() -> None:
    assert currencies("Top up: 5 EUR") == {"EUR"}
    assert currencies("ALL items 5 EUR") == {"EUR"}
    assert currencies("USD 5") == currencies("5 USD") == currencies("(5.00 USD)") == {"USD"}
    assert currencies("USD $5") == {"USD", "$"}  # a symbol may stand between
    assert currencies("usd 5") == set()
    assert currencies("Pay in EUR: 5") == set()  # a word between: not next to the number


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("Top up: 5 EUR", Decimal(5)),
        ("ALL items 5 EUR", Decimal(5)),
        ("(5.00 EUR)", Decimal(-5)),
        ("-USD5", Decimal(-5)),
        ("5 XYZ", Decimal(5)),  # not an ISO code: just a word
    ],
)
def test_parse_number_reads_the_same_currency_codes(text: str, number: Decimal) -> None:
    assert parse_number(text) == number


@pytest.mark.parametrize("text", ["usd 5", "5 eur", "5 Eur", "top 10"])
def test_parse_number_refuses_a_code_that_is_not_upper_case(text: str) -> None:
    # Lower case is not a code, but the amount's currency is unclear (fail closed).
    assert parse_number(text) is None
