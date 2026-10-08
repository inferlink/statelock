# SPDX-License-Identifier: Apache-2.0
"""Versioned artifact record schema.

An ActionRecord is what the proxy hands to an ArtifactSink for each governed
action. ``payload`` is the redacted JSON body stored as ``context.json``;
``screenshots`` holds the raw image bytes keyed by file name. Sinks and sink
wrappers (for example, a hash chain) may add top-level keys to the
payload; the keys written by build_action_record are reserved.

Schema history (all changes so far are additive: readers of "1" can read "2"):
- "1": initial schema.
- "2": ``context.tenant_id``; ``context.remembered`` (remembered policy fields);
  ``params.statelock_uploads`` on DOM.setFileInputFiles; ``params.statelock_redacted``
  when typed secrets are masked; download records (``Statelock.download``, kind
  ``download``); ``target_element.input_type`` / ``autocomplete``.
- "3": ``review`` (human review of a paused action: id, status, reviewer, comment,
  times, the failed rules) and its ``review-screenshot.jpg``; verdict decision
  ``review`` is never stored (a stored verdict is the outcome: allow or block);
  ``verdict.evidence.review_deferred`` for actions allowed while a review rule failed.
- "4": ``policy_id`` (in verdicts, violations, review failures and
  ``post_conditions_evaluated``) names the policy, not the agent: the policy's ``id``
  from the policy file, or ``<agent_id>#<n>`` for the agent's n-th policy.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from pydantic import BaseModel, Field

from statelock.audit.redaction import redact_payload
from statelock.core.state import ActionContext
from statelock.core.verdict import PolicyVerdict
from statelock.review.queue import REVIEW_SCREENSHOT_FILE, Review

SCHEMA_VERSION = "4"

SCREENSHOT_FILES = {
    "browser_state": "screenshot.jpg",
    "post_browser_state": "post-screenshot.jpg",
}


class ActionRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    session_id: str
    sequence: int
    payload: dict[str, Any]
    screenshots: dict[str, bytes] = Field(default_factory=dict)


def build_action_record(
    context: ActionContext,
    verdict: PolicyVerdict,
    post_verdict: PolicyVerdict | None = None,
    review: Review | None = None,
) -> ActionRecord:
    context_payload = context.model_dump(mode="json")
    screenshots: dict[str, bytes] = {}
    for state_key, filename in SCREENSHOT_FILES.items():
        state = context_payload.get(state_key)
        if not isinstance(state, dict):
            continue
        encoded = state.pop("screenshot_base64", None)
        state["screenshot_file"] = None
        if encoded:
            try:
                screenshots[filename] = base64.b64decode(encoded)
                state["screenshot_file"] = filename
            except (binascii.Error, ValueError):
                state["screenshot_error"] = "invalid base64 screenshot"

    review_block = review.evidence() if review is not None else None
    if review is not None and review.screenshot:
        screenshots[REVIEW_SCREENSHOT_FILE] = review.screenshot

    payload = redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "context": context_payload,
            "verdict": verdict.model_dump(mode="json"),
            "post_verdict": post_verdict.model_dump(mode="json") if post_verdict else None,
            "policy_violation": verdict.violation_summary(),
            "post_policy_violation": post_verdict.violation_summary() if post_verdict else None,
            "review": review_block,
        }
    )
    return ActionRecord(
        session_id=context.session_id,
        sequence=context.sequence,
        payload=payload,
        screenshots=screenshots,
    )


def build_network_record(record: dict[str, Any]) -> dict[str, Any]:
    """Redacted network log line."""
    return redact_payload({"schema_version": SCHEMA_VERSION, **record})
