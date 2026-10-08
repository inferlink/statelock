# SPDX-License-Identifier: Apache-2.0
"""The session's DownloadPolicy: downloads the page starts, governed like actions."""

from __future__ import annotations

from typing import Any

from statelock.core.actions import DOWNLOAD_METHOD, ActionKind, CdpAction
from statelock.core.enums import SystemRule
from statelock.core.state import ActionContext
from statelock.core.verdict import PolicyVerdict
from statelock.policy.evaluator import PolicyEvaluator
from statelock.proxy.downloads import DownloadCheck
from statelock.proxy.governor import ActionGovernor
from statelock.proxy.inspector import Inspector


def _without_review(verdict: PolicyVerdict) -> PolicyVerdict:
    """Downloads cannot wait for a reviewer: a review verdict blocks them."""
    if not verdict.needs_review:
        return verdict
    return PolicyVerdict.block(
        reason=f"{verdict.reason} (a download cannot wait for review)",
        rule=verdict.rule or "",
        policy_id=verdict.policy_id,
        evidence=verdict.evidence,
    )


def _download_check(verdict: PolicyVerdict) -> DownloadCheck:
    if not verdict.blocked:
        return DownloadCheck(allowed=True)
    return DownloadCheck(allowed=False, rule=verdict.rule or None)


def _download_action(params: dict[str, Any]) -> CdpAction:
    return CdpAction(message_id=None, method=DOWNLOAD_METHOD, kind=ActionKind.DOWNLOAD, params=params)


class SessionDownloadPolicy:
    """Checks each download (proxy.downloads.DownloadPolicy) through the session's governor:
    captured, evaluated, recorded, and the session ends when one is blocked."""

    def __init__(self, governor: ActionGovernor, inspector: Inspector, evaluator: PolicyEvaluator) -> None:
        self.governor = governor
        self.inspector = inspector
        self.evaluator = evaluator

    async def download_begin(self, params: dict[str, Any], frame_id: str | None) -> tuple[DownloadCheck, str | None]:
        async def decide(context: ActionContext) -> PolicyVerdict:
            return _without_review(await self.evaluator.evaluate(context))

        # A download from an in-process iframe has no target of its own: use the tab heuristic.
        target_id = await self.inspector.resolve_frame_target(frame_id)
        verdict, url = await self.governor.govern_page_action(_download_action(params), target_id, decide)
        return _download_check(verdict), url

    async def download_complete(self, params: dict[str, Any], begin_url: str | None) -> DownloadCheck:
        async def decide(context: ActionContext) -> PolicyVerdict:
            return _without_review(await self.evaluator.evaluate_download_completion(context, begin_url))

        verdict, _ = await self.governor.govern_page_action(_download_action(params), None, decide)
        return _download_check(verdict)

    async def download_limit(self, reason: str, params: dict[str, Any]) -> None:
        async def decide(_context: ActionContext) -> PolicyVerdict:
            return PolicyVerdict.block(
                reason=f"Blocked download: {reason}",
                rule=SystemRule.DOWNLOAD_LIMIT.value,
                evidence={"download": params},
            )

        await self.governor.govern_page_action(_download_action(params), None, decide)
