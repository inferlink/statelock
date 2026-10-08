# SPDX-License-Identifier: Apache-2.0
"""A custom Statelock rule for the OJS example: decline only what the decision source says.

The agent decides which papers to decline and writes the email, but a misled or
buggy agent could decline the wrong paper or send other text. This rule asks the
decision source itself, at the moment the agent clicks, so Statelock (not the
agent) enforces it:

    - trigger: {click_text: [Decline Submission]}
      ojs_decision: {paper_field: url_paper_id}
    - trigger: {click_text: [Record Editorial Decision]}
      ojs_decision: {paper_field: url_paper_id, email_field: decline_email}

It passes only when the source says to reject that paper, has an email for it, and
(with ``email_field``) the email typed into the page contains that email.

The demo's source is a JSON file (``demo_decisions.json``: {"<paper id>": {"reject": true,
"email": "..."}}). For a real journal, replace ``read_decision`` with a call to the
spreadsheet or service holding the editors' decisions; keep it in ``asyncio.to_thread``
if it blocks.

Load it into the proxy with ``STATELOCK_RULE_MODULES=examples/ojs/ojs_rules.py``
(compose.dev.yaml does). If it raises or is slow, Statelock blocks the click.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from statelock.policy.rules import Rule, RuleContext, RuleFailure, register_rule
from statelock.policy.text import normalize_text

DEFAULT_DECISIONS = Path(__file__).with_name("demo_decisions.json")
MIN_EMAIL_CHARS = 20


def read_decision(decisions_file: Path, paper_id: int) -> dict[str, Any] | None:
    """The decision for one paper, from the demo's JSON file (None: no decision)."""
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    decision = decisions.get(str(paper_id)) if isinstance(decisions, dict) else None
    return decision if isinstance(decision, dict) else None


@register_rule
class OjsDecision(Rule):
    rule_name: ClassVar[str] = "ojs_decision"
    phases: ClassVar[frozenset[str]] = frozenset({"pre"})
    check_timeout: ClassVar[float | None] = 10.0

    # The field with the paper id (e.g. taken from the URL).
    paper_field: str
    # The field with the email typed into the page; when set, it must contain the decision's email.
    email_field: str | None = None
    # Relative paths are next to this module.
    decisions_file: str = Field(default=DEFAULT_DECISIONS.name)

    def _path(self) -> Path:
        path = Path(self.decisions_file)
        return path if path.is_absolute() else Path(__file__).with_name(self.decisions_file)

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        refs = (self.paper_field,) if self.email_field is None else (self.paper_field, self.email_field)
        evidence = ctx.field_evidence(*refs)
        raw_id = ctx.field(self.paper_field)
        if raw_id is None or not raw_id.isdigit():
            return RuleFailure(f"Blocked: no paper id in {self.paper_field} to check the decision for", evidence)
        paper_id = int(raw_id)
        decision = await asyncio.to_thread(read_decision, self._path(), paper_id)
        evidence["decision"] = {"paper_id": paper_id, "found": decision is not None}
        if decision is None or decision.get("reject") is not True:
            return RuleFailure(f"Blocked: the decision source does not say to decline paper {paper_id}", evidence)
        email = decision.get("email")
        if not isinstance(email, str) or len(email.strip()) < MIN_EMAIL_CHARS:
            return RuleFailure(f"Blocked: the decision source has no usable email for paper {paper_id}", evidence)
        if self.email_field is not None:
            typed = ctx.field(self.email_field) or ""
            if normalize_text(email) not in normalize_text(typed):
                return RuleFailure(
                    f"Blocked: the email in the page is not the one the decision source has for paper {paper_id}",
                    evidence,
                )
        return None
