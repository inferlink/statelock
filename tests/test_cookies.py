"""cookie_access in policies: validation, merging, filtering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from statelock.policy.cookies import CookieAccess
from statelock.policy.evaluator import PolicyEvaluator
from statelock.policy.models import PolicyBundle

COOKIES = [
    {"name": "session", "domain": "app.example.com", "value": "s"},
    {"name": "csrftoken", "domain": ".app.example.com", "value": "c"},
    {"name": "px", "domain": ".img.cdn.example.net", "value": "p"},
]


def test_names_and_domains_select_cookies() -> None:
    access = CookieAccess(names=["csrftoken"], domains=[".cdn.example.net"])
    allowed, withheld = access.filter(COOKIES)
    assert [c["name"] for c in allowed] == ["csrftoken", "px"]
    assert [c["name"] for c in withheld] == ["session"]
    assert CookieAccess(all=True).filter(COOKIES)[1] == []


def test_empty_cookie_access_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs names, domains or all"):
        CookieAccess()


def test_access_is_the_union_of_the_agents_policies() -> None:
    bundle = PolicyBundle.model_validate(
        {
            "policies": [
                {"agent_id": "a", "target_url_contains": "/x", "cookie_access": {"names": ["one"]}},
                {"agent_id": "a", "target_url_contains": "/y", "cookie_access": {"domains": ["b.test"]}},
                {"agent_id": "other", "cookie_access": {"all": True}},
                {"agent_id": "none"},
            ]
        }
    )
    evaluator = PolicyEvaluator(bundle)
    access = evaluator.cookie_access("a")
    assert access is not None and access.names == ["one"] and access.domains == ["b.test"] and not access.all
    assert evaluator.cookie_access("none") is None
