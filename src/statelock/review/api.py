# SPDX-License-Identifier: Apache-2.0
"""Review API and the built-in review page.

All endpoints need a reviewer key (``Authorization: Bearer``; agent keys are
refused) and show only the reviewer's tenant:

- ``GET /reviews``: pending reviews (``?status=approved|denied|expired|cancelled|all``).
- ``GET /reviews/<id>``: one review, with the paused action and the failed rules.
- ``GET /reviews/<id>/screenshot``: the page as the reviewer should judge it (JPEG).
- ``POST /reviews/<id>/decision``: ``{"decision": "approve" | "deny", "comment": "..."}``.
- ``GET /review``: a page for reviewers (the key is entered there and kept in the tab).
"""

from __future__ import annotations

import logging
from importlib import resources
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from statelock.auth import AUTHORIZATION_HEADER, AuthError, Reviewer
from statelock.review.queue import ReviewError, ReviewQueue, ReviewStatus
from statelock.services import get_services
from statelock.wire import REVIEW_ENDPOINT_PREFIX, REVIEW_PAGE_PATH, REVIEWER_ID_HEADER

logger = logging.getLogger(__name__)

router = APIRouter()


def _queue(request: Request) -> ReviewQueue:
    return get_services(request.app).reviews


def authenticated_reviewer(request: Request) -> Reviewer:
    """FastAPI dependency: the calling reviewer. 401 without a valid reviewer key."""
    authenticator = get_services(request.app).authenticator
    try:
        return authenticator.authenticate_reviewer(
            request.headers.get(AUTHORIZATION_HEADER), request.headers.get(REVIEWER_ID_HEADER)
        )
    except AuthError as error:
        logger.warning("Rejected %s: reviewer authentication failed: %s", request.url.path, error)
        raise HTTPException(
            status_code=401, detail="Reviewer authentication required", headers={"WWW-Authenticate": "Bearer"}
        ) from error


ReviewerDep = Annotated[Reviewer, Depends(authenticated_reviewer)]
StatusFilter = Literal["pending", "approved", "denied", "expired", "cancelled", "all"]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]
    comment: str | None = Field(default=None, max_length=2000)


def _http_error(error: ReviewError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=str(error))


@router.get(REVIEW_ENDPOINT_PREFIX)
async def list_reviews(request: Request, reviewer: ReviewerDep, status: StatusFilter = "pending") -> dict[str, Any]:
    selected = None if status == "all" else ReviewStatus(status)
    return {"reviews": [review.summary() for review in _queue(request).find(reviewer, selected)]}


@router.get(REVIEW_ENDPOINT_PREFIX + "/{review_id}")
async def get_review(review_id: str, request: Request, reviewer: ReviewerDep) -> dict[str, Any]:
    try:
        return _queue(request).get(review_id, reviewer).summary()
    except ReviewError as error:
        raise _http_error(error) from error


@router.get(REVIEW_ENDPOINT_PREFIX + "/{review_id}/screenshot")
async def get_review_screenshot(review_id: str, request: Request, reviewer: ReviewerDep) -> Response:
    try:
        review = _queue(request).get(review_id, reviewer)
    except ReviewError as error:
        raise _http_error(error) from error
    if review.screenshot is None:
        raise HTTPException(status_code=404, detail="No screenshot for this review")
    return Response(content=review.screenshot, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post(REVIEW_ENDPOINT_PREFIX + "/{review_id}/decision")
async def decide_review(review_id: str, body: DecisionBody, request: Request, reviewer: ReviewerDep) -> dict[str, Any]:
    try:
        review = await _queue(request).decide(
            review_id, reviewer, approve=body.decision == "approve", comment=body.comment
        )
    except ReviewError as error:
        raise _http_error(error) from error
    logger.info("Review %s %s by %s", review_id, review.status.value, reviewer.reviewer_id)
    return review.summary()


@router.get(REVIEW_PAGE_PATH, response_class=HTMLResponse)
async def review_page() -> HTMLResponse:
    html = resources.files("statelock.review").joinpath("page.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
