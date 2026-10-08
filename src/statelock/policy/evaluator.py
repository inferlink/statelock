# SPDX-License-Identifier: Apache-2.0
"""Evaluate pre- and post-conditions for governed actions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from statelock.core.enums import SystemRule, ViolationType
from statelock.core.state import ActionContext, BrowserState
from statelock.core.verdict import PolicyVerdict
from statelock.fileio import load_yaml_file
from statelock.policy.cookies import CookieAccess
from statelock.policy.fields import ScopedField
from statelock.policy.models import PolicyBundle, PolicyConfig
from statelock.policy.perception import NotConfiguredPerceptionEvaluator, PerceptionEvaluator
from statelock.policy.requests import RequestAccess
from statelock.policy.rules import Rule, RuleContext, RuleFailure

logger = logging.getLogger(__name__)


def _read_policies(path: Path) -> list[Any]:
    """The policy entries of one file: either {policies: [...]} or a single policy mapping."""
    if not path.exists():
        raise FileNotFoundError(f"Statelock policy file not found: {path}")
    payload = load_yaml_file(path) or {}
    if isinstance(payload, dict) and "policies" in payload:
        policies = payload["policies"] or []
        return policies if isinstance(policies, list) else [policies]
    return [payload]


def load_policy_bundle(*paths: Path) -> PolicyBundle:
    """Load one or more policy files as one bundle (checked together: policy ids are
    unique across files, and an agent's fields mean the same in all of them)."""
    return PolicyBundle.model_validate({"policies": [entry for path in paths for entry in _read_policies(path)]})


class PolicyEvaluator:
    def __init__(self, bundle: PolicyBundle, perception: PerceptionEvaluator | None = None) -> None:
        if not isinstance(bundle, PolicyBundle):
            # Policy ids and field names are only checked by PolicyBundle.
            raise TypeError(f"PolicyEvaluator needs a PolicyBundle, got {type(bundle).__name__}")
        self.policies: list[PolicyConfig] = list(bundle.policies)
        self.perception: PerceptionEvaluator = perception or NotConfiguredPerceptionEvaluator()

    @classmethod
    def from_files(cls, paths: list[Path], perception: PerceptionEvaluator | None = None) -> PolicyEvaluator:
        return cls(load_policy_bundle(*paths), perception)

    # Registration and guard scope ---------------------------------------------------

    @property
    def registered_agent_ids(self) -> set[str]:
        return {policy.agent_id for policy in self.policies}

    def is_registered(self, agent_id: str | None) -> bool:
        return agent_id is not None and agent_id in self.registered_agent_ids

    def request_access(self, agent_id: str | None) -> RequestAccess:
        """The requests Statelock may make for this agent: the union of its policies' request_access."""
        return RequestAccess([rule for p in self.policies if p.agent_id == agent_id for rule in p.request_access])

    def cookie_access(self, agent_id: str | None) -> CookieAccess | None:
        """The cookies this agent may read: the union of its policies' cookie_access."""
        return CookieAccess.merge(
            [p.cookie_access for p in self.policies if p.agent_id == agent_id and p.cookie_access is not None]
        )

    def guard_url_patterns(self, agent_id: str | None) -> list[str] | None:
        """URL substrings on which the page guard enforces for this agent.

        None means every page (a guarded policy has no target_url_contains).
        An empty list means no page is guarded.
        """
        patterns: list[str] = []
        for policy in self.policies:
            if policy.agent_id != agent_id or policy.allow_synthetic_events:
                continue
            if not policy.target_url_contains:
                return None
            patterns.append(policy.target_url_contains)
        return patterns

    def field_specs(self, agent_id: str | None) -> list[ScopedField]:
        """This agent's named fields, each scoped to its url_contains or its policy's target."""
        scoped: list[ScopedField] = []
        for policy in self.policies:
            if policy.agent_id != agent_id:
                continue
            for spec in policy.fields:
                scope = spec.url_contains if spec.url_contains is not None else policy.target_url_contains
                field = ScopedField(spec=spec, url_contains=scope or None)
                if field not in scoped:
                    scoped.append(field)
        return scoped

    # Evaluation ---------------------------------------------------------------------

    def _registration_failure(self, context: ActionContext) -> tuple[str, str] | None:
        if self.is_registered(context.agent_id):
            return None
        return (
            f"Blocked action because agent is not registered in the policy bundle: {context.agent_id}",
            SystemRule.AGENT_REGISTRATION.value,
        )

    def _gate_failure(self, context: ActionContext, state: BrowserState | None) -> tuple[str, str] | None:
        """Checks that fail closed before any policy rule is evaluated."""
        if registration := self._registration_failure(context):
            return registration
        if state is None:
            return ("Blocked action because browser state was not captured.", SystemRule.STATE_CAPTURE.value)
        if state.capture_error:
            return (
                f"Blocked action because browser state capture failed: {state.capture_error}",
                SystemRule.STATE_CAPTURE.value,
            )
        return None

    async def _pre_verdict(
        self,
        context: ActionContext,
        url: str | None,
        rule_context: RuleContext,
        evidence: dict[str, Any],
        select: Callable[[Rule], bool] = lambda _rule: True,
    ) -> PolicyVerdict | None:
        """Run the selected pre-conditions of the policies for url.

        The first failing on_fail: block rule blocks. Failures of on_fail: review
        rules are collected: a review verdict lists them all, so an approval covers
        exactly what the reviewer saw. None when every rule passed.
        """
        review_failures: list[dict[str, Any]] = []
        for policy in self.policies:
            if not policy.applies_to(context.agent_id, url):
                continue
            for rule in policy.pre_conditions:
                if not select(rule):
                    continue
                if rule.trigger is not None and not rule.trigger.matches_before(
                    context, context.browser_state, is_activation=rule_context.is_activation
                ):
                    continue  # a triggered pre-condition checks only the activations it names
                failure = await check_rule(rule, rule_context)
                if failure is None:
                    continue
                if rule.needs_review:
                    review_failures.append(
                        {
                            "policy_id": policy.policy_id,
                            "rule": rule.rule_name,
                            "reason": failure.reason,
                            "evidence": failure.evidence,
                        }
                    )
                    continue
                return PolicyVerdict.block(
                    reason=failure.reason,
                    rule=rule.rule_name,
                    policy_id=policy.policy_id,
                    evidence={**evidence, **failure.evidence},
                )
        return PolicyVerdict.review(review_failures, evidence) if review_failures else None

    async def evaluate(self, context: ActionContext) -> PolicyVerdict:
        state = context.browser_state
        evidence: dict[str, Any] = {"context": context.summary(state)}
        gate = self._gate_failure(context, state)
        if gate is not None:
            reason, gate_rule = gate
            return PolicyVerdict.block(reason=reason, rule=gate_rule, evidence=evidence)
        rule_context = RuleContext(action=context, state=state, phase="pre", perception=self.perception)
        verdict = await self._pre_verdict(context, state.url if state else None, rule_context, evidence)
        return verdict or PolicyVerdict.allow("No policy rule blocked this action.", evidence)

    async def evaluate_download_completion(self, context: ActionContext, begin_url: str | None) -> PolicyVerdict:
        """Rules marked checked_on_download_completion (restrict_downloads) of the policies
        that applied where the download began. The page may have changed since, so the
        other pre-conditions are not re-run."""
        evidence: dict[str, Any] = {"context": context.summary(), "begin_url": begin_url}
        if registration := self._registration_failure(context):
            reason, gate_rule = registration
            return PolicyVerdict.block(reason=reason, rule=gate_rule, evidence=evidence)
        rule_context = RuleContext(action=context, state=context.browser_state, phase="pre", perception=self.perception)
        blocked = await self._pre_verdict(
            context, begin_url, rule_context, evidence, lambda rule: rule.checked_on_download_completion
        )
        return blocked or PolicyVerdict.allow("Download completed and passed restrict_downloads.", evidence)

    async def evaluate_post(self, context: ActionContext) -> PolicyVerdict | None:
        post_state = context.post_browser_state
        if post_state is None:
            return None
        evidence: dict[str, Any] = {"context": context.summary(post_state)}
        gate = self._gate_failure(context, post_state)
        if gate is not None:
            reason, gate_rule = gate
            return PolicyVerdict.block(
                reason=reason, rule=gate_rule, violation_type=ViolationType.POST_CONDITION, evidence=evidence
            )

        # Applicability and triggers use the pre-action state: the post-condition
        # belongs to the action taken on that page, even if the click navigated.
        pre_state = context.browser_state
        pre_url = pre_state.url if pre_state else None
        evaluated: list[dict[str, Any]] = []
        evidence["post_conditions_evaluated"] = evaluated
        rule_context = RuleContext(action=context, state=post_state, phase="post", perception=self.perception)

        for policy in self.policies:
            if not policy.applies_to(context.agent_id, pre_url):
                continue
            for index, rule in enumerate(policy.post_conditions):
                triggered = rule.trigger is None or rule.trigger.matches(context, pre_state)
                evaluated.append({"policy_id": policy.policy_id, "index": index, "triggered": triggered})
                if not triggered:
                    continue
                failure = await check_rule(rule, rule_context)
                if failure is not None:
                    return PolicyVerdict.block(
                        reason=failure.reason,
                        rule=rule.rule_name,
                        violation_type=ViolationType.POST_CONDITION,
                        policy_id=policy.policy_id,
                        evidence={**evidence, **failure.evidence},
                    )
        return PolicyVerdict.allow("No post-condition failed.", evidence)


async def check_rule(rule: Rule, ctx: RuleContext) -> RuleFailure | None:
    """Run one rule. A rule that raises or takes longer than its check_timeout fails:
    a broken custom rule blocks the action instead of letting it through."""
    try:
        if rule.check_timeout is None:
            return await rule.check(ctx)
        return await asyncio.wait_for(rule.check(ctx), timeout=rule.check_timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return RuleFailure(f"Blocked action because rule {rule.rule_name} did not answer within {rule.check_timeout} s")
    except Exception as error:
        logger.exception("Rule %s failed", rule.rule_name)
        return RuleFailure(
            f"Blocked action because rule {rule.rule_name} could not be evaluated: {type(error).__name__}: {error}"
        )
