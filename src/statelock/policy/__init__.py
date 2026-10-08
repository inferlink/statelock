# SPDX-License-Identifier: Apache-2.0
"""Policy schema, rules and evaluation."""

from statelock.policy.evaluator import PolicyEvaluator, check_rule, load_policy_bundle
from statelock.policy.fields import FieldSpec, RememberedValue, ScopedField, SessionMemory, parse_number
from statelock.policy.models import PolicyBundle, PolicyConfig
from statelock.policy.perception import (
    NotConfiguredPerceptionEvaluator,
    PerceptionEvaluator,
    PerceptionRequest,
    PerceptionVerdict,
)
from statelock.policy.rules import (
    RULES,
    AssertCompare,
    AssertFieldEqual,
    FileRestriction,
    ProhibitClickText,
    ProhibitPageText,
    RequirePageText,
    RestrictDownloads,
    RestrictUploads,
    Rule,
    RuleContext,
    RuleFailure,
    VisualAssert,
    parse_rule,
    register_rule,
)
from statelock.policy.triggers import ActionTrigger

__all__ = [
    "RULES",
    "ActionTrigger",
    "AssertCompare",
    "AssertFieldEqual",
    "FieldSpec",
    "FileRestriction",
    "NotConfiguredPerceptionEvaluator",
    "PerceptionEvaluator",
    "PerceptionRequest",
    "PerceptionVerdict",
    "PolicyBundle",
    "PolicyConfig",
    "PolicyEvaluator",
    "ProhibitClickText",
    "ProhibitPageText",
    "RememberedValue",
    "RequirePageText",
    "RestrictDownloads",
    "RestrictUploads",
    "Rule",
    "RuleContext",
    "RuleFailure",
    "ScopedField",
    "SessionMemory",
    "VisualAssert",
    "check_rule",
    "load_policy_bundle",
    "parse_number",
    "parse_rule",
    "register_rule",
]
