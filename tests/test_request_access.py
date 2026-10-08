"""Policy request_access (no browser)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from statelock.policy.models import PolicyBundle
from statelock.policy.requests import RequestAccess, RequestRule


def test_rules_match_method_and_url() -> None:
    access = RequestAccess(
        [
            RequestRule(url_pattern=r"^https://a\.test/api/"),
            RequestRule(url_pattern=r"^https://b\.test/upload$", methods=["post"]),
        ]
    )
    assert access.rule_for("GET", "https://a.test/api/x") is not None
    assert access.rule_for("POST", "https://a.test/api/x") is None  # GET only by default
    assert access.rule_for("POST", "https://b.test/upload") is not None
    assert access.rule_for("GET", "https://evil.test/?u=https://a.test/api/") is None


@pytest.mark.parametrize(
    "rule",
    [
        {"url_pattern": "^https://("},
        {"url_pattern": "^https://x/", "methods": ["FETCH"]},
        {"url_pattern": "^https://x/", "methods": []},
        {"url_pattern": "^https://x/", "max_response_bytes": 99_000_000},
        {"url_pattern": "^https://x/", "extra": 1},
        {"url_pattern": "^https://x/", "follow_redirects": True},
    ],
)
def test_invalid_rules_are_rejected(rule: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RequestRule.model_validate(rule)


@pytest.mark.parametrize(
    "pattern",
    ["", "/api/", "a\\.test/api/", "https://a\\.test/", "^/api/", ".*", "^https://a.*", "^https://a\\.test\\..*/"],
)
def test_url_pattern_must_be_anchored_to_a_scheme(pattern: str) -> None:
    # Unanchored, "/api/" would also allow https://elsewhere.example/?/api/ (or, empty, every URL).
    with pytest.raises(ValidationError, match="url_pattern"):
        RequestRule(url_pattern=pattern)


def test_policy_accepts_request_access() -> None:
    bundle = PolicyBundle.model_validate(
        {
            "policies": [
                {
                    "agent_id": "a",
                    "request_access": [{"url_pattern": "^https?://a\\.test/api/", "methods": ["GET", "POST"]}],
                }
            ]
        }
    )
    assert bundle.policies[0].request_access[0].methods == ["GET", "POST"]


PORTAL = r"^https://portal\.example\.com/"


@pytest.mark.parametrize(
    "pattern",
    [
        PORTAL + "|evil",  # a second branch without the host
        r"^https://portal\.example\.com/api/|^https://evil\.test/",
        r"^https://portal\.example\.com/a|b|c",
    ],
)
def test_url_pattern_with_top_level_alternation_is_rejected(pattern: str) -> None:
    with pytest.raises(ValidationError, match="'\\|'"):
        RequestRule(url_pattern=pattern)


def test_alternation_inside_a_group_or_class_is_allowed() -> None:
    rule = RequestRule(url_pattern=PORTAL + r"(?:api|files)/[a|b]")
    assert rule.allows("GET", "https://portal.example.com/files/a")
    assert rule.allows("GET", "https://portal.example.com/api/|")
    assert not rule.allows("GET", "https://evil.test/api/a")


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.test/evil",
        "https://portal.example.com.evil.net/",  # the host as a prefix of another
        "https://portal.example.com@evil.net/",  # the host as user info
        "https://user@portal.example.com/",  # user info before the host
        "https://evil.net\\@portal.example.com/",  # a backslash the browser reads as '/'
        "https://evil.net/https://portal.example.com/",
        "https://portal.example.com:443/",  # an explicit port the pattern does not name
        "https://portal.example.com:8443/",
        "https://portal.example.com./",  # trailing-dot host
        "https://portal%2Eexample.com/",
        " https://portal.example.com/",
        "http://portal.example.com/",
        "https://portal.exampl\u0435.com/",  # Cyrillic "e"
    ],
)
def test_requests_off_the_pinned_origin_are_declined(url: str) -> None:
    assert not RequestRule(url_pattern=PORTAL).allows("GET", url)


def test_host_letters_compare_in_ascii_only() -> None:
    # The Kelvin sign lowercases to "k"; only the ASCII host is accepted.
    rule = RequestRule(url_pattern=r"^https://bank\.example/")
    assert rule.allows("GET", "https://bank.example/x")
    assert not rule.allows("GET", "https://ban\u212a.example/x")


def test_ports_are_pinned() -> None:
    exact = RequestRule(url_pattern=r"^https://a\.test:8443/")
    assert exact.allows("GET", "https://a.test:8443/x")
    assert not exact.allows("GET", "https://a.test:8444/x")
    assert not exact.allows("GET", "https://a.test/x")
    assert not exact.allows("GET", "https://a.test:8443@evil.test/x")
    any_port = RequestRule(url_pattern=r"^https?://a\.test:[0-9]+/")
    assert any_port.allows("GET", "http://a.test:81/x")
    assert any_port.allows("GET", "https://a.test:8443/x")
    assert not any_port.allows("GET", "https://a.test/x")
    assert not any_port.allows("GET", "https://a.test:/x")
    assert not any_port.allows("GET", "https://a.test:\u0661/x")  # Arabic-Indic digit one


def test_scheme_choice_is_pinned() -> None:
    both = RequestRule(url_pattern=r"^https?://a\.test/")
    assert both.allows("GET", "http://a.test/") and both.allows("GET", "https://a.test/")
    assert not RequestRule(url_pattern=r"^https://a\.test/").allows("GET", "http://a.test/")


@pytest.mark.parametrize("rest", [r"a\|b", "[]|]", "[^]|]x", r"(a|b)\)"])
def test_literal_pipes_are_not_alternation(rest: str) -> None:
    RequestRule(url_pattern=PORTAL + rest)
