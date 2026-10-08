# SPDX-License-Identifier: Apache-2.0
"""Policy file schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator, model_validator

from statelock.core.urls import origin_and_path, page_address, url_in_scope
from statelock.policy.cookies import CookieAccess
from statelock.policy.fields import FieldSpec
from statelock.policy.requests import RequestRule
from statelock.policy.rules import Rule, parse_rule

POLICY_ID_PATTERN = r"^[A-Za-z0-9_.:#-]{1,100}$"


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Names the policy in verdicts, violations and evidence. Default ``<agent_id>#<n>``:
    # the agent's n-th policy in the file (1-based). Unique in the bundle.
    id: str | None = Field(default=None, pattern=POLICY_ID_PATTERN)
    agent_id: str
    # A substring of the page's scheme://host/path (never its query or fragment).
    target_url_contains: str | None = None
    # False (default): clicks, submits and requests not produced by real input
    # or the site's own code are blocked on this policy's pages.
    allow_synthetic_events: bool = False
    # Named page values for rules; see statelock.policy.fields.
    fields: list[FieldSpec] = Field(default_factory=list)
    pre_conditions: list[SerializeAsAny[Rule]] = Field(default_factory=list)
    # Checked after commit actions (release, tap, drop, Enter), on the page the action led to.
    post_conditions: list[SerializeAsAny[Rule]] = Field(default_factory=list)
    # Browser cookies this agent may read; none by default (see statelock.policy.cookies).
    cookie_access: CookieAccess | None = None
    # HTTP requests Statelock may make for this agent in the page; none by default
    # (see statelock.policy.requests).
    request_access: list[RequestRule] = Field(default_factory=list)

    @field_validator("pre_conditions", mode="before")
    @classmethod
    def _parse_pre(cls, value: Any) -> Any:
        return [parse_rule(entry, "pre") for entry in value] if isinstance(value, list) else value

    @field_validator("post_conditions", mode="before")
    @classmethod
    def _parse_post(cls, value: Any) -> Any:
        return [parse_rule(entry, "post") for entry in value] if isinstance(value, list) else value

    @property
    def policy_id(self) -> str:
        """The policy's id (set for every policy of a bundle; see PolicyBundle)."""
        return self.id or self.agent_id

    def applies_to(self, agent_id: str | None, url: str | None) -> bool:
        if self.agent_id != agent_id:
            return False
        if not self.target_url_contains:
            return True
        if not url:
            return False
        address = page_address(url)
        # A URL that does not parse gets the policy (fail closed).
        return address is None or url_in_scope(self.target_url_contains, url)


class PolicyBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policies: list[PolicyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _policy_ids(self) -> PolicyBundle:
        """Give each policy without an id ``<agent_id>#<n>``; every id names one policy."""
        count: dict[str, int] = {}
        seen: set[str] = set()
        for policy in self.policies:
            count[policy.agent_id] = count.get(policy.agent_id, 0) + 1
            if policy.id is None:
                policy.id = f"{policy.agent_id}#{count[policy.agent_id]}"
            if policy.id in seen:
                raise ValueError(f"policy id {policy.id} is used by more than one policy")
            seen.add(policy.id)
        return self

    @model_validator(mode="after")
    def _unique_field_names(self) -> PolicyBundle:
        """A field name means one thing per agent, so rules and memory are unambiguous."""
        seen: dict[tuple[str, str], FieldSpec] = {}
        for policy in self.policies:
            for spec in policy.fields:
                key = (policy.agent_id, spec.name)
                previous = seen.get(key)
                if previous is not None and previous != spec:
                    raise ValueError(f"agent {policy.agent_id}: field {spec.name} is defined twice differently")
                seen[key] = spec
        return self

    @model_validator(mode="after")
    def _remembered_fields_pinned(self) -> PolicyBundle:
        """A remembered value is carried to other pages, so it must come from a known site:
        an absolute URL scope or allowed_origins."""
        for policy in self.policies:
            for spec in policy.fields:
                if not spec.remember or spec.allowed_origins:
                    continue
                parsed = origin_and_path(spec.url_contains or policy.target_url_contains or "")
                if parsed is None or not parsed[0].startswith(("http://", "https://")):
                    raise ValueError(
                        f"agent {policy.agent_id}: remembered field {spec.name} needs an absolute URL scope "
                        "or allowed_origins"
                    )
        return self

    @model_validator(mode="after")
    def _remembered_references(self) -> PolicyBundle:
        """Every ``remembered.<name>`` a rule reads is a ``remember: true`` field of the agent,
        so a typo is refused here rather than blocking every action at runtime."""
        remembered: dict[str, set[str]] = {}
        for policy in self.policies:
            names = remembered.setdefault(policy.agent_id, set())
            names.update(spec.name for spec in policy.fields if spec.remember)
        for policy in self.policies:
            for rule in (*policy.pre_conditions, *policy.post_conditions):
                unknown = sorted(rule.remembered_names() - remembered[policy.agent_id])
                if unknown:
                    raise ValueError(
                        f"policy {policy.policy_id}: rule {rule.rule_name} reads "
                        f"{', '.join('remembered.' + name for name in unknown)}, but agent {policy.agent_id} "
                        "has no remember: true field of that name"
                    )
        return self
