"""Human review: policy syntax, verdicts, activation points, the queue, reviewer keys, the API."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from helpers import URL, context_for, key_action, mouse_action, run
from pydantic import ValidationError

from statelock import events as event_names
from statelock.__main__ import main
from statelock.app import create_app
from statelock.audit.records import SCHEMA_VERSION, build_action_record
from statelock.auth import Authenticator, AuthError, KeysFile, Reviewer, hash_key
from statelock.core.actions import ActionKind, CdpAction
from statelock.core.enums import Decision
from statelock.core.verdict import PolicyVerdict
from statelock.events import Events
from statelock.policy.evaluator import PolicyEvaluator
from statelock.policy.models import PolicyBundle, PolicyConfig
from statelock.review import ReviewError, ReviewQueue, ReviewStatus, describe_action, fingerprints
from statelock.settings import Settings

POLICY = {
    "agent_id": "agent",
    "target_url_contains": "/demo/finance",
    "pre_conditions": [
        {"assert_compare": {"left": "amount", "op": "<=", "value": 1000, "on_fail": "review"}},
        {"require_page_text": {"values": ["Invoice"], "on_fail": "review"}},
        {"prohibit_click_text": {"values": ["Delete"]}},
    ],
}
ACME = Reviewer(reviewer_id="alice", tenant="acme", authenticated=True)
OTHER = Reviewer(reviewer_id="bob", tenant="other", authenticated=True)


def _evaluate(element: dict[str, Any] | None = None, **state: Any) -> PolicyVerdict:
    context = context_for(mouse_action(), element=element or {"text": "Pay"}, **state)
    return run(PolicyEvaluator(PolicyBundle.model_validate({"policies": [POLICY]})).evaluate(context))


# Policy syntax -------------------------------------------------------------------------


def test_on_fail_defaults_to_block() -> None:
    policy = PolicyConfig.model_validate(POLICY)
    assert [rule.on_fail for rule in policy.pre_conditions] == ["review", "review", "block"]


def test_on_fail_review_is_refused_in_post_conditions() -> None:
    entry = {**POLICY, "post_conditions": [{"require_page_text": {"values": ["Done"], "on_fail": "review"}}]}
    with pytest.raises(ValidationError, match="not allowed in post-conditions"):
        PolicyConfig.model_validate(entry)


def test_on_fail_review_is_refused_for_downloads() -> None:
    entry = {**POLICY, "pre_conditions": [{"restrict_downloads": {"extensions": [".pdf"], "on_fail": "review"}}]}
    with pytest.raises(ValidationError, match="not supported"):
        PolicyConfig.model_validate(entry)


def test_on_fail_rejects_unknown_values() -> None:
    entry = {**POLICY, "pre_conditions": [{"require_page_text": {"values": ["x"], "on_fail": "ask"}}]}
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(entry)


# Verdicts ------------------------------------------------------------------------------


def test_review_verdict_lists_every_review_failure() -> None:
    verdict = _evaluate(extracted_fields={"amount": "$5,000"}, page_text="Receipt")
    assert verdict.decision == Decision.REVIEW
    assert verdict.needs_review and not verdict.blocked
    assert [f["rule"] for f in verdict.review_failures] == ["assert_compare", "require_page_text"]
    assert verdict.rule == "assert_compare"
    assert verdict.violation_summary() is None


def test_blocking_rule_wins_over_review() -> None:
    verdict = _evaluate(element={"text": "Delete"}, extracted_fields={"amount": "$5,000"}, page_text="Invoice")
    assert verdict.blocked
    assert verdict.rule == "prohibit_click_text"


def test_passing_rules_allow() -> None:
    assert _evaluate(extracted_fields={"amount": "$500"}, page_text="Invoice").decision == Decision.ALLOW


def test_fingerprints_include_the_reason() -> None:
    low = _evaluate(extracted_fields={"amount": "$5,000"}, page_text="Invoice")
    high = _evaluate(extracted_fields={"amount": "$9,000"}, page_text="Invoice")
    assert fingerprints(low) != fingerprints(high)
    assert fingerprints(low) == fingerprints(_evaluate(extracted_fields={"amount": "$5,000"}, page_text="Invoice"))


# Activation points ---------------------------------------------------------------------


def _key(event_type: str, key: str, code: str, modifiers: int = 0) -> CdpAction:
    action = key_action(event_type, key, code)
    return CdpAction(action.message_id, action.method, action.kind, {**action.params, "modifiers": modifiers}, "S1")


def test_activation_points() -> None:
    assert mouse_action("mousePressed").starts_activation
    assert mouse_action("mouseReleased").ends_activation
    assert not mouse_action("mouseMoved").starts_activation
    assert not mouse_action("mouseMoved").ends_activation
    assert _key("keyDown", "Enter", "Enter").starts_activation
    assert _key("keyUp", "Enter", "Enter").ends_activation
    assert not _key("keyDown", "a", "KeyA").starts_activation
    assert _key("keyDown", "s", "KeyS", modifiers=2).starts_activation  # Ctrl+S
    assert not _key("keyDown", "A", "KeyA", modifiers=8).starts_activation  # Shift only
    upload = CdpAction(1, "DOM.setFileInputFiles", ActionKind.FILE_UPLOAD, {}, "S1")
    assert upload.starts_activation and upload.release_key is None
    text = CdpAction(1, "Input.insertText", ActionKind.TEXT, {"text": "5000"}, "S1")
    assert not text.starts_activation and not text.ends_activation


def test_press_and_release_share_a_release_key() -> None:
    assert mouse_action("mousePressed").release_key == mouse_action("mouseReleased").release_key
    assert mouse_action("mousePressed").release_key != mouse_action("mousePressed", session_id="S2").release_key
    assert _key("keyDown", "Enter", "Enter").release_key == _key("keyUp", "Enter", "Enter").release_key


def test_describe_action() -> None:
    context = context_for(mouse_action(), element={"text": "Mark as Paid", "tag_name": "BUTTON"})
    assert describe_action(context) == 'click "Mark as Paid"'
    enter = context_for(key_action(), element={"tag_name": "INPUT"})
    assert describe_action(enter) == "press Enter on INPUT"
    upload = CdpAction(1, "DOM.setFileInputFiles", ActionKind.FILE_UPLOAD, {"statelock_uploads": [{"name": "a.pdf"}]})
    assert describe_action(context_for(upload)) == "upload a.pdf to the page"
    unnamed = CdpAction(1, "DOM.setFileInputFiles", ActionKind.FILE_UPLOAD, {"statelock_uploads": []})
    assert describe_action(context_for(unnamed)) == "upload files to the page"
    tap = CdpAction(1, "Input.dispatchTouchEvent", ActionKind.TOUCH, {"type": "touchStart"})
    assert describe_action(context_for(tap, element={"text": "Pay"})) == 'tap "Pay"'


# Queue ---------------------------------------------------------------------------------


def _review_context(tenant: str = "acme") -> Any:
    context = context_for(mouse_action(), element={"text": "Pay"}, screenshot_base64="aGVsbG8=")
    context.tenant_id = tenant
    return context


def _review_verdict() -> PolicyVerdict:
    failure = {"policy_id": "agent", "rule": "assert_compare", "reason": "amount $5,000 is not <= 1000", "evidence": {}}
    return PolicyVerdict.review([failure], {})


def test_queue_approve_wakes_the_waiting_action() -> None:
    events = Events()
    seen: list[tuple[str, str]] = []

    async def record(name: str, payload: dict[str, Any]) -> None:
        seen.append((name, payload["status"]))

    events.subscribe(event_names.REVIEW_REQUESTED, lambda p: record("requested", p))
    events.subscribe(event_names.REVIEW_DECIDED, lambda p: record("decided", p))

    async def scenario() -> Any:
        queue = ReviewQueue(events)
        review = await queue.open(_review_context(), _review_verdict(), timeout=5)
        assert [r.review_id for r in queue.find(ACME)] == [review.review_id]
        assert queue.find(OTHER) == []
        waiter = asyncio.ensure_future(queue.wait(review, asyncio.Event()))
        await asyncio.sleep(0)
        await queue.decide(review.review_id, ACME, approve=True, comment="ok")
        done = await waiter
        with pytest.raises(ReviewError) as again:
            await queue.decide(review.review_id, ACME, approve=False, comment=None)
        assert again.value.status == 409
        return done

    review = run(scenario())
    assert review.status == ReviewStatus.APPROVED
    assert review.reviewer_id == "alice"
    assert review.screenshot == b"hello"
    assert seen == [("requested", "pending"), ("decided", "approved")]


def test_queue_hides_other_tenants() -> None:
    async def scenario() -> None:
        queue = ReviewQueue(Events())
        review = await queue.open(_review_context(), _review_verdict(), timeout=5)
        for call in (lambda: queue.get(review.review_id, OTHER),):
            with pytest.raises(ReviewError) as missing:
                call()
            assert missing.value.status == 404
        with pytest.raises(ReviewError):
            await queue.decide(review.review_id, OTHER, approve=True, comment=None)
        assert review.pending

    run(scenario())


def test_queue_expires_and_cancels() -> None:
    async def scenario() -> tuple[ReviewStatus, ReviewStatus, ReviewStatus]:
        queue = ReviewQueue(Events())
        expired = await queue.wait(await queue.open(_review_context(), _review_verdict(), 0.05), asyncio.Event())
        ending = asyncio.Event()
        review = await queue.open(_review_context(), _review_verdict(), 5)
        waiter = asyncio.ensure_future(queue.wait(review, ending))
        await asyncio.sleep(0)
        ending.set()
        cancelled = await waiter
        disconnected = await queue.open(_review_context(), _review_verdict(), 5)
        task = asyncio.ensure_future(queue.wait(disconnected, asyncio.Event()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return expired.status, cancelled.status, disconnected.status

    assert run(scenario()) == (ReviewStatus.EXPIRED, ReviewStatus.CANCELLED, ReviewStatus.CANCELLED)


def test_queue_keeps_pending_reviews_when_trimming() -> None:
    async def scenario() -> ReviewQueue:
        queue = ReviewQueue(Events(), history_size=1)
        first = await queue.open(_review_context(), _review_verdict(), 5)
        second = await queue.open(_review_context(), _review_verdict(), 5)
        await queue.decide(second.review_id, ACME, approve=False, comment=None)
        await queue.open(_review_context(), _review_verdict(), 5)
        assert queue.get(first.review_id, ACME).pending
        return queue

    queue = run(scenario())
    assert [r.status for r in queue.find(ACME, None)] == [ReviewStatus.PENDING, ReviewStatus.PENDING]


def test_record_stores_the_review() -> None:
    async def scenario() -> Any:
        queue = ReviewQueue(Events())
        review = await queue.open(_review_context(), _review_verdict(), 5)
        await queue.decide(review.review_id, ACME, approve=True, comment="fine")
        return review

    review = run(scenario())
    record = build_action_record(_review_context(), PolicyVerdict.allow("approved"), review=review)
    assert record.schema_version == SCHEMA_VERSION
    assert record.payload["review"]["status"] == "approved"
    assert record.payload["review"]["failures"][0]["rule"] == "assert_compare"
    assert record.screenshots["review-screenshot.jpg"] == b"hello"
    assert build_action_record(_review_context(), PolicyVerdict.allow("ok")).payload["review"] is None


# Reviewer keys -------------------------------------------------------------------------


def _authenticator() -> Authenticator:
    return Authenticator(
        "api_key",
        KeysFile.model_validate(
            {
                "agents": [{"agent_id": "agent", "tenant": "acme", "key_sha256": hash_key("slk_agent")}],
                "reviewers": [{"reviewer_id": "alice", "tenant": "acme", "key_sha256": hash_key("slk_alice")}],
            }
        ),
    )


def test_reviewer_and_agent_keys_are_separate() -> None:
    auth = _authenticator()
    assert auth.authenticate_reviewer("Bearer slk_alice") == Reviewer("alice", "acme", True)
    with pytest.raises(AuthError, match="agent key used as reviewer key"):
        auth.authenticate_reviewer("Bearer slk_agent")
    with pytest.raises(AuthError, match="reviewer key used as agent key"):
        auth.authenticate(None, "Bearer slk_alice")
    assert auth.authenticate(None, "Bearer slk_agent").agent_id == "agent"


def test_keys_file_rejects_a_key_shared_by_agent_and_reviewer() -> None:
    digest = hash_key("slk_same")
    with pytest.raises(ValidationError, match="appears twice"):
        KeysFile.model_validate(
            {
                "agents": [{"agent_id": "agent", "key_sha256": digest}],
                "reviewers": [{"reviewer_id": "alice", "key_sha256": digest}],
            }
        )


def test_reviewer_without_authentication_sees_every_tenant() -> None:
    reviewer = Authenticator("none").authenticate_reviewer(None, "carol")
    assert reviewer.reviewer_id == "carol" and reviewer.may_review("anything")


def test_keygen_for_a_reviewer(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keygen", "--reviewer", "alice", "--tenant", "acme"]) == 0
    captured = capsys.readouterr()
    entry = yaml.safe_load(captured.out)["reviewers"][0]
    assert entry["reviewer_id"] == "alice" and entry["tenant"] == "acme"
    assert entry["key_sha256"] == hash_key(captured.err.split()[-1])


# API -----------------------------------------------------------------------------------


@pytest.fixture
def review_api(tmp_path: Path, policy_file: Path) -> Iterator[tuple[TestClient, ReviewQueue]]:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        yaml.safe_dump(
            {
                "agents": [{"agent_id": "agent", "tenant": "acme", "key_sha256": hash_key("slk_agent")}],
                "reviewers": [
                    {"reviewer_id": "alice", "tenant": "acme", "key_sha256": hash_key("slk_alice")},
                    {"reviewer_id": "bob", "tenant": "other", "key_sha256": hash_key("slk_bob")},
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        policy_file=policy_file, artifact_dir=tmp_path / "artifacts", plugins="none", auth_keys_file=keys
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.services.reviews


def _as(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_api_lists_and_decides(review_api: tuple[TestClient, ReviewQueue]) -> None:
    client, queue = review_api
    review = client.portal.call(queue.open, _review_context(), _review_verdict(), 30)  # type: ignore[union-attr]
    assert client.get("/reviews").status_code == 401
    assert client.get("/reviews", headers=_as("slk_agent")).status_code == 401
    assert client.get("/reviews", headers=_as("slk_bob")).json() == {"reviews": []}

    listed = client.get("/reviews", headers=_as("slk_alice")).json()["reviews"]
    assert [r["review_id"] for r in listed] == [review.review_id]
    assert listed[0]["action"]["description"] == 'click "Pay"'
    assert listed[0]["page"]["url"] == URL

    shot = client.get(f"/reviews/{review.review_id}/screenshot", headers=_as("slk_alice"))
    assert shot.status_code == 200 and shot.content == b"hello"
    assert client.get(f"/reviews/{review.review_id}", headers=_as("slk_bob")).status_code == 404
    assert (
        client.post(
            f"/reviews/{review.review_id}/decision", headers=_as("slk_bob"), json={"decision": "approve"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/reviews/{review.review_id}/decision", headers=_as("slk_alice"), json={"decision": "maybe"}
        ).status_code
        == 422
    )

    decided = client.post(
        f"/reviews/{review.review_id}/decision", headers=_as("slk_alice"), json={"decision": "deny", "comment": "no"}
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "denied" and decided.json()["reviewer_id"] == "alice"
    again = client.post(f"/reviews/{review.review_id}/decision", headers=_as("slk_alice"), json={"decision": "approve"})
    assert again.status_code == 409
    assert client.get("/reviews", headers=_as("slk_alice")).json() == {"reviews": []}
    assert len(client.get("/reviews?status=denied", headers=_as("slk_alice")).json()["reviews"]) == 1


def test_review_page_is_served(review_api: tuple[TestClient, ReviewQueue]) -> None:
    client, _ = review_api
    page = client.get("/review")
    assert page.status_code == 200
    assert "Statelock review" in page.text
