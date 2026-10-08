# SPDX-License-Identifier: Apache-2.0
"""Paused actions waiting for a human reviewer.

A rule with ``on_fail: review`` pauses the agent's action instead of ending the
session. The governor opens a review here and waits; a reviewer approves or
denies it through the review API (statelock.review.api). A review that is not
decided within ``STATELOCK_REVIEW_TIMEOUT`` expires, and the action is blocked.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from statelock import events as event_names
from statelock.auth import Reviewer
from statelock.core.actions import ActionKind
from statelock.core.state import ActionContext
from statelock.core.verdict import PolicyVerdict
from statelock.events import Events

REVIEW_SCREENSHOT_FILE = "review-screenshot.jpg"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"  # no decision within the review timeout
    CANCELLED = "cancelled"  # the session ended while the action waited


class ReviewError(Exception):
    """A decision the queue refuses. ``status`` is the HTTP status for the API."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


Fingerprint = tuple[str | None, str | None, str | None]


def fingerprints(verdict: PolicyVerdict) -> frozenset[Fingerprint]:
    """What a reviewer approves: each failed review rule and its reason (which names the values)."""
    return frozenset(
        (failure.get("policy_id"), failure.get("rule"), failure.get("reason")) for failure in verdict.review_failures
    )


def describe_action(context: ActionContext) -> str:
    """One line for the reviewer, e.g. ``click "Mark as Paid"``."""
    state = context.browser_state
    target = state.target_element if state else None
    label = target.label_text if target else ""
    params = context.params
    kind = context.action_kind
    verbs = {ActionKind.MOUSE.value: "click", ActionKind.TOUCH.value: "tap", ActionKind.DRAG.value: "drop onto"}
    verb = verbs.get(kind)
    if kind == ActionKind.KEYBOARD.value:
        verb = f"press {params.get('key') or params.get('code') or 'a key'} on"
    elif kind == ActionKind.FILE_UPLOAD.value:
        uploads = params.get("statelock_uploads") or []
        names = [str(upload.get("name")) for upload in uploads if isinstance(upload, dict)]
        verb = f"upload {', '.join(names)} to" if names else "upload files to"
    subject = f'"{label[:120]}"' if label else (target.tag_name or "the page") if target else "the page"
    return f"{verb or context.method} {subject}"


@dataclass
class Review:
    review_id: str
    context: ActionContext
    verdict: PolicyVerdict
    requested_at: datetime
    expires_at: datetime
    screenshot: bytes | None = None
    status: ReviewStatus = ReviewStatus.PENDING
    decided_at: datetime | None = None
    reviewer_id: str | None = None
    comment: str | None = None
    _decided: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def tenant_id(self) -> str | None:
        return self.context.tenant_id

    @property
    def pending(self) -> bool:
        return self.status == ReviewStatus.PENDING

    def _close(self, status: ReviewStatus, reviewer_id: str | None = None, comment: str | None = None) -> None:
        self.status = status
        self.decided_at = datetime.now(timezone.utc)
        self.reviewer_id = reviewer_id
        self.comment = comment
        self._decided.set()

    def evidence(self) -> dict[str, Any]:
        """The review block stored with the action record."""
        return {
            "review_id": self.review_id,
            "status": self.status.value,
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "reviewer_id": self.reviewer_id,
            "comment": self.comment,
            "failures": [
                {key: failure.get(key) for key in ("policy_id", "rule", "reason")}
                for failure in self.verdict.review_failures
            ],
            "screenshot_file": REVIEW_SCREENSHOT_FILE if self.screenshot else None,
        }

    def summary(self) -> dict[str, Any]:
        """What the review API returns: the review block plus the paused action."""
        state = self.context.browser_state
        target = state.target_element if state else None
        return {
            **self.evidence(),
            "session_id": self.context.session_id,
            "agent_id": self.context.agent_id,
            "tenant_id": self.tenant_id,
            "sequence": self.context.sequence,
            "action": {
                "description": describe_action(self.context),
                "method": self.context.method,
                "action_kind": self.context.action_kind,
                "params": self.context.params,
                "target_element": target.model_dump(exclude_none=True) if target else None,
            },
            "page": {
                "url": state.url if state else None,
                "title": state.title if state else None,
                "fields": state.extracted_fields if state else {},
            },
            "remembered": self.context.remembered,
            "failures": self.verdict.review_failures,
            "has_screenshot": self.screenshot is not None,
        }


class ReviewQueue:
    """Process-wide: open reviews by id, and a bounded history of decided ones."""

    def __init__(self, events: Events, history_size: int = 1000) -> None:
        self.events = events
        self.history_size = history_size
        self._reviews: OrderedDict[str, Review] = OrderedDict()

    async def open(self, context: ActionContext, verdict: PolicyVerdict, timeout: float) -> Review:
        now = datetime.now(timezone.utc)
        review = Review(
            review_id=uuid.uuid4().hex,
            context=context,
            verdict=verdict,
            requested_at=now,
            expires_at=now + timedelta(seconds=timeout),
            screenshot=_decode_screenshot(context),
        )
        self._reviews[review.review_id] = review
        self._trim()
        await self.events.emit(event_names.REVIEW_REQUESTED, review.summary())
        return review

    async def wait(self, review: Review, cancelled: asyncio.Event) -> Review:
        """Until decided, expired, or ``cancelled`` is set (the session is ending)."""
        remaining = (review.expires_at - datetime.now(timezone.utc)).total_seconds()
        decided = asyncio.ensure_future(review._decided.wait())
        ended = asyncio.ensure_future(cancelled.wait())
        try:
            await asyncio.wait({decided, ended}, timeout=max(0.0, remaining), return_when=asyncio.FIRST_COMPLETED)
        finally:
            decided.cancel()
            ended.cancel()
            if review.pending:
                # Timed out, the session ended, or this task was cancelled (agent disconnected).
                timed_out = _expired(review) and not cancelled.is_set()
                review._close(ReviewStatus.EXPIRED if timed_out else ReviewStatus.CANCELLED)
                await self.events.emit(event_names.REVIEW_DECIDED, review.summary())
        return review

    def get(self, review_id: str, reviewer: Reviewer) -> Review:
        review = self._reviews.get(review_id)
        # Another tenant's review looks absent.
        if review is None or not reviewer.may_review(review.tenant_id):
            raise ReviewError(404, "No such review")
        return review

    def find(self, reviewer: Reviewer, status: ReviewStatus | None = ReviewStatus.PENDING) -> list[Review]:
        return [
            review
            for review in self._reviews.values()
            if reviewer.may_review(review.tenant_id) and (status is None or review.status == status)
        ]

    async def decide(self, review_id: str, reviewer: Reviewer, *, approve: bool, comment: str | None) -> Review:
        review = self.get(review_id, reviewer)
        if not review.pending:
            raise ReviewError(409, f"Review is already {review.status.value}")
        if _expired(review):
            raise ReviewError(409, "Review has expired")
        review._close(ReviewStatus.APPROVED if approve else ReviewStatus.DENIED, reviewer.reviewer_id, comment)
        await self.events.emit(event_names.REVIEW_DECIDED, review.summary())
        return review

    def _trim(self) -> None:
        """Drop the oldest decided reviews beyond history_size. Pending ones stay."""
        excess = len(self._reviews) - self.history_size
        for review_id in [key for key, review in self._reviews.items() if not review.pending][: max(0, excess)]:
            del self._reviews[review_id]


def _expired(review: Review) -> bool:
    return datetime.now(timezone.utc) >= review.expires_at


def _decode_screenshot(context: ActionContext) -> bytes | None:
    encoded = context.browser_state.screenshot_base64 if context.browser_state else None
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded)
    except (binascii.Error, ValueError):
        return None
