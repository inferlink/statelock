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
