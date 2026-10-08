# SPDX-License-Identifier: Apache-2.0
"""Allow/block verdicts shared by the policy engine, proxy and sinks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from statelock.core.enums import Decision, ViolationType


class PolicyVerdict(BaseModel):
    decision: Decision
    reason: str = ""
    violation_type: ViolationType | None = None
    rule: str | None = None
    policy_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.decision == Decision.BLOCK

    @property
    def needs_review(self) -> bool:
        return self.decision == Decision.REVIEW

    @property
    def review_failures(self) -> list[dict[str, Any]]:
        """For a review verdict: every failed on_fail: review rule (policy_id, rule, reason, evidence)."""
        failures = self.evidence.get("review_failures")
        return failures if isinstance(failures, list) else []

    @classmethod
    def allow(cls, reason: str, evidence: dict[str, Any] | None = None) -> PolicyVerdict:
        return cls(decision=Decision.ALLOW, reason=reason, evidence=evidence or {})

    @classmethod
    def block(
        cls,
        *,
        reason: str,
        rule: str,
        violation_type: ViolationType = ViolationType.PRE_CONDITION,
        policy_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> PolicyVerdict:
        return cls(
            decision=Decision.BLOCK,
            reason=reason,
            rule=rule,
            violation_type=violation_type,
            policy_id=policy_id,
            evidence=evidence or {},
        )

    @classmethod
    def review(cls, failures: list[dict[str, Any]], evidence: dict[str, Any]) -> PolicyVerdict:
        """Rules with on_fail: review failed (and no blocking rule did)."""
        first = failures[0]
        return cls(
            decision=Decision.REVIEW,
            reason=first["reason"],
            rule=first["rule"],
            violation_type=ViolationType.PRE_CONDITION,
            policy_id=first["policy_id"],
            evidence={**evidence, "review_failures": failures},
        )

    def violation_summary(self) -> dict[str, Any] | None:
        if not self.blocked:
            return None
        return {
            "violation_type": self.violation_type.value if self.violation_type else None,
            "rule": self.rule,
            "reason": self.reason,
            "policy_id": self.policy_id,
        }
