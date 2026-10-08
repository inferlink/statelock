# SPDX-License-Identifier: Apache-2.0
"""Govern each intercepted action: capture, evaluate, record, release or block."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from statelock import events as event_names
from statelock.audit.records import build_action_record, build_network_record
from statelock.audit.redaction import SECRET_MARKER, mask_secret_input
from statelock.audit.sequencing import SequencedWriter, Slot
from statelock.audit.sink import ArtifactSink
from statelock.core.actions import (
    FOCUS_METHODS,
    IME_METHOD,
    TEXT_METHOD,
    ActionKind,
    CdpAction,
    KeyPresses,
    is_activation_key,
    is_enter_key,
    protocol_action,
)
from statelock.core.enums import SystemRule, TargetSelection
from statelock.core.jsonutil import as_int, parse_cdp_message
from statelock.core.state import ActionContext, BrowserState
from statelock.core.verdict import PolicyVerdict
from statelock.credentials import InjectionError, SecretScrubber, SecretStore
from statelock.events import Events
from statelock.policy.evaluator import PolicyEvaluator
from statelock.policy.fields import SessionMemory
from statelock.proxy.connection import CdpChannel
from statelock.proxy.guard import violation_method, violation_reason
from statelock.proxy.inspector import Inspector
from statelock.proxy.reporter import ViolationReporter
from statelock.proxy.targets import TargetRegistry
from statelock.review.queue import Fingerprint, Review, ReviewQueue, ReviewStatus, fingerprints
from statelock.settings import Settings
from statelock.wire import CLOSE_CODE_POST_CONDITION, CLOSE_CODE_PRE_CONDITION

logger = logging.getLogger(__name__)

HeldKey = tuple[str | None, int]
# Typed text in which {{secret:name}} placeholders are replaced (Playwright fill sends insertText).
SECRET_METHODS = frozenset({TEXT_METHOD, IME_METHOD})


@dataclass(frozen=True)
class _Approval:
    release_key: tuple[str, ...]
    fingerprints: frozenset[Fingerprint]
    review: Review


def _approved(verdict: PolicyVerdict, review: Review) -> PolicyVerdict:
    evidence = {**verdict.evidence, "review": review.evidence()}
    return PolicyVerdict.allow(f"Approved by reviewer {review.reviewer_id}: {verdict.reason}", evidence)


def _refused(verdict: PolicyVerdict, review: Review) -> PolicyVerdict:
    """A denied, expired or cancelled review blocks the action."""
    outcome = (
        f"Denied by reviewer {review.reviewer_id}"
        if review.status == ReviewStatus.DENIED
        else f"Review {review.status.value}"
    )
    return PolicyVerdict.block(
        reason=f"{outcome}: {verdict.reason}",
        rule=verdict.rule or "",
        policy_id=verdict.policy_id,
        evidence={**verdict.evidence, "review": review.evidence()},
    )


def _deferred(verdict: PolicyVerdict) -> PolicyVerdict:
    """Not an activation: allowed; the review happens when the agent activates something."""
    evidence = {key: value for key, value in verdict.evidence.items() if key != "review_failures"}
    evidence["review_deferred"] = verdict.review_failures
    return PolicyVerdict.allow(f"Allowed; review deferred to the next activation: {verdict.reason}", evidence)


class ActionGovernor:
    """Per-session pipeline for everything that is governed and recorded.

    Each governed event is captured, evaluated and written in sequence order:
    the agent's input actions (``govern_input``), commands Statelock answers or
    refuses, page guard violations, and what the page starts (``govern_page_action``,
    used by SessionDownloadPolicy). Replayed script clicks (ScriptClickReplayer)
    use the same steps. Remembered policy fields are updated from every capture
    (see FieldMemoryWatcher for captures outside actions).

    It forwards governed input on the agent's channel to the browser
    (``agent_channel``) and answers the agent on its WebSocket (``client_ws``).
    """

    def __init__(
        self,
        *,
        session_id: str,
        agent_id: str | None,
        settings: Settings,
        tenant_id: str | None = None,
        evaluator: PolicyEvaluator,
        sink: ArtifactSink,
        events: Events,
        inspector: Inspector,
        targets: TargetRegistry,
        reporter: ViolationReporter,
        reviews: ReviewQueue,
        agent_channel: CdpChannel,
        client_ws: WebSocket,
        scrubber: SecretScrubber,
        memory: SessionMemory | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.settings = settings
        self.evaluator = evaluator
        self.sink = sink
        self.events = events
        self.inspector = inspector
        self.targets = targets
        self.reporter = reporter
        self.reviews = reviews
        self.agent_channel = agent_channel
        self.client_ws = client_ws
        self.secrets = secrets or SecretStore()
        # Values of the secrets this session injected: removed from the evidence and from
        # everything the agent receives (the session's scrubber, shared with the reporter and the bridge).
        self.scrubber = scrubber
        # An approved press: its matching release passes without a second review.
        self._approval: _Approval | None = None
        self.writer = SequencedWriter(sink, events, scrub=scrubber.data)
        self.memory = memory or SessionMemory([])
        self._held_responses: dict[HeldKey, asyncio.Future[str]] = {}
        # Pairs a char with the rawKeyDown that pressed its key (one activation, one commit).
        self._keys = KeyPresses()

    # Capture --------------------------------------------------------------------------

    async def capture(self, action: CdpAction, target_id: str | None = None) -> BrowserState:
        """Capture from target_id (a tab Statelock acts on itself, e.g. a replayed click), else
        from the tab the agent's action is dispatched to (fail closed if unknown), else the
        heuristic tab (the action has no CDP session)."""
        if target_id is None and action.session_id is not None:
            target_id = self.targets.target_for(action.session_id)
            if target_id is None:
                return BrowserState(
                    target_selection=TargetSelection.ACTION_SESSION,
                    capture_error=f"Unknown CDP session for action: {action.session_id}",
                )
        return await self.inspector.capture(action, target_id=target_id)

    def _context(self, sequence: int, action: CdpAction, state: BrowserState) -> ActionContext:
        context = ActionContext.from_action(
            session_id=self.session_id,
            sequence=sequence,
            action=action,
            browser_state=state,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
        )
        context.remembered = self.memory.snapshot()
        # Typing into a password, one-time-code or card field, or into a frame Statelock
        # cannot read (it may be one): keep the text out of the evidence. Enter (and Space,
        # which activates in a frame) stays visible: rules and triggers check those keys.
        target = state.target_element
        if target is not None and target.source == "focus" and action.method in FOCUS_METHODS:
            keeps_key = is_activation_key(action.params) if target.unresolved else is_enter_key(action.params)
            secret = (target.is_secret_input or target.unresolved) and not keeps_key
            context.params = mask_secret_input(context.params, secret_target=secret)
        return context

    # Input actions --------------------------------------------------------------------

    async def govern_input(self, action: CdpAction, payload: dict[str, Any]) -> bool:
        """Govern one intercepted input action (``payload``: the agent's command, as evaluated).
        Returns False when the session ends."""
        action = self._keys.annotate(action)
        async with self.writer.slot() as slot:
            context, verdict, review = await self.decide_input(slot, action)
            state = context.browser_state
            logger.info(
                "Intercepted %s action #%s session=%s method=%s verdict=%s url=%s",
                action.kind.value,
                context.sequence,
                self.session_id,
                action.method,
                verdict.decision.value,
                state.url if state else None,
            )
            if self.reporter.terminating:
                # A guard violation ended the session while this action was evaluated
                # (or waited for review): record it, and answer with that violation.
                await slot.write(build_action_record(context, verdict, review=review))
                await self.reporter.refuse({"id": action.message_id, "sessionId": action.session_id})
                return False
            if not verdict.blocked:
                payload, verdict = self._inject_secrets(action, context, verdict, payload)
            if verdict.blocked:
                await slot.write(build_action_record(context, verdict, review=review))
                await self.reporter.reject(action, context, verdict, CLOSE_CODE_PRE_CONDITION)
                return False

            if not action.is_commit:
                self.agent_channel.send(json.dumps(payload))
                await slot.write(build_action_record(context, verdict, review=review))
                return True

            return await self._govern_commit(slot, action, context, verdict, review=review, payload=payload)

    def _inject_secrets(
        self, action: CdpAction, context: ActionContext, verdict: PolicyVerdict, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], PolicyVerdict]:
        """Replace {{secret:name}} placeholders in typed text, where the secrets file allows it.

        Returns the command to forward and the verdict (a block when a placeholder is not allowed).
        """
        text = action.params.get("text")
        if action.method not in SECRET_METHODS or not isinstance(text, str) or not SecretStore.placeholders(text):
            return payload, verdict
        state = context.browser_state
        target = state.target_element if state else None
        try:
            injected, names = self.secrets.inject(
                text,
                agent_id=self.agent_id,
                url=state.url if state else None,
                password_field=bool(target is not None and target.source == "focus" and target.is_secret_input),
            )
        except InjectionError as error:
            context.params = {**context.params, "text": SECRET_MARKER}
            blocked = PolicyVerdict.block(
                reason=f"Blocked a secret placeholder: {error}",
                rule=SystemRule.SECRET_INJECTION.value,
                evidence={"context": context.summary()},
            )
            return payload, blocked
        for name in names:
            self.scrubber.add(self.secrets.value(name))
        context.params = {**context.params, "text": SECRET_MARKER, "statelock_secrets": names}
        logger.info("Injected secret(s) %s session=%s", ", ".join(names), self.session_id)
        return {**payload, "params": {**action.params, "text": injected}}, verdict

    async def _capture_and_evaluate(
        self, slot: Slot, action: CdpAction, target_id: str | None = None
    ) -> tuple[ActionContext, PolicyVerdict]:
        state = await self.capture(action, target_id)
        self.memory.update(state, "action", slot.sequence)
        context = self._context(slot.sequence, action, state)
        return context, await self.evaluator.evaluate(context)

    # Human review ---------------------------------------------------------------------

    async def decide_input(
        self, slot: Slot, action: CdpAction, target_id: str | None = None
    ) -> tuple[ActionContext, PolicyVerdict, Review | None]:
        """Evaluate; when an on_fail: review rule failed, pause for a reviewer.

        Only activations wait (a press, tap, drop, upload, Enter/Space or shortcut,
        and an uncovered release): other actions are allowed and the failure is
        recorded as review_deferred. An approved press covers its matching release.
        """
        if action.starts_activation:
            self._approval = None
        context, verdict = await self._capture_and_evaluate(slot, action, target_id)
        if not verdict.needs_review:
            return context, verdict, None
        approval = self._approval
        if (
            approval is not None
            and action.ends_activation
            and action.release_key == approval.release_key
            and fingerprints(verdict) <= approval.fingerprints
        ):
            self._approval = None
            return context, _approved(verdict, approval.review), approval.review
        if not (action.starts_activation or action.ends_activation):
            return context, _deferred(verdict), None
        return await self._review(slot, action, context, verdict, target_id)

    async def _review(
        self,
        slot: Slot,
        action: CdpAction,
        context: ActionContext,
        verdict: PolicyVerdict,
        target_id: str | None = None,
    ) -> tuple[ActionContext, PolicyVerdict, Review]:
        review = await self.reviews.open(context, verdict, self.settings.review_timeout)
        logger.warning(
            "Paused %s action #%s session=%s for review %s: %s",
            action.method,
            context.sequence,
            self.session_id,
            review.review_id,
            verdict.reason,
        )
        await self.reviews.wait(review, self.reporter.terminated)
        logger.info("Review %s %s session=%s", review.review_id, review.status.value, self.session_id)
        if review.status != ReviewStatus.APPROVED:
            return context, _refused(verdict, review), review

        # The page may have changed while the action waited: check it again.
        context, current = await self._capture_and_evaluate(slot, action, target_id)
        if current.blocked:
            return context, current, review
        approved = fingerprints(verdict)
        if current.needs_review and not fingerprints(current) <= approved:
            changed = PolicyVerdict.block(
                reason=f"Blocked after review: the page changed while the action waited ({current.reason})",
                rule=current.rule or verdict.rule or "",
                policy_id=current.policy_id,
                evidence={**current.evidence, "review": review.evidence()},
            )
            return context, changed, review
        if action.starts_activation and action.release_key is not None:
            self._approval = _Approval(action.release_key, approved, review)
        return context, _approved(current, review), review

    async def _govern_commit(
        self,
        slot: Slot,
        action: CdpAction,
        context: ActionContext,
        verdict: PolicyVerdict,
        *,
        review: Review | None,
        payload: dict[str, Any],
    ) -> bool:
        """Commit actions (mouseReleased, Enter keyUp): hold the browser's response until
        post-conditions pass, so a failure is returned on the agent's own call."""
        held_response = await self._forward_and_hold(action, json.dumps(payload))
        post_verdict = await self.post_check(slot, action, context)
        await slot.write(build_action_record(context, verdict, post_verdict, review=review))
        if post_verdict is not None and post_verdict.blocked:
            await self.reporter.reject(action, context, post_verdict, CLOSE_CODE_POST_CONDITION)
            return False
        if held_response is not None and not self.reporter.client_closed:
            await self.client_ws.send_text(self.scrubber.text(held_response))
        return True

    async def post_check(
        self, slot: Slot, action: CdpAction, context: ActionContext, target_id: str | None = None
    ) -> PolicyVerdict | None:
        """After a commit: let the page settle, capture it, remember its fields, run post-conditions."""
        await asyncio.sleep(self.settings.post_capture_settle)
        context.post_browser_state = await self.capture(action, target_id)
        self.memory.update(context.post_browser_state, "action_post", slot.sequence)
        return await self.evaluator.evaluate_post(context)

    async def _forward_and_hold(self, action: CdpAction, message: str) -> str | None:
        if action.message_id is None:
            self.agent_channel.send(message)
            return None
        key: HeldKey = (action.session_id, action.message_id)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._held_responses[key] = future
        try:
            self.agent_channel.send(message)
            return await asyncio.wait_for(future, timeout=self.settings.held_response_timeout)
        except asyncio.TimeoutError:
            logger.warning("No browser response for held action in session %s", self.session_id)
            return None
        finally:
            self._held_responses.pop(key, None)

    def claim_held_response(self, raw_message: str) -> bool:
        """Route a browser response to a waiting commit action, if one is held."""
        if not self._held_responses:
            return False
        payload = parse_cdp_message(raw_message)
        message_id = as_int((payload or {}).get("id"))
        if payload is None or message_id is None:
            return False
        session_id = payload.get("sessionId")
        future = self._held_responses.get((session_id if isinstance(session_id, str) else None, message_id))
        if future is None or future.done():
            return False
        future.set_result(raw_message)
        return True

    # Commands Statelock answers itself or refuses ------------------------------------

    async def record_command(self, payload: dict[str, Any], verdict: PolicyVerdict) -> None:
        """Record a command Statelock answered itself (allowed or declined), without
        ending the session. The caller answers the agent."""
        action = protocol_action(payload)
        _context, location = await self._record_command(action, verdict)
        logger.info(
            "Answered CDP command session=%s method=%s decision=%s at %s",
            self.session_id,
            action.method,
            verdict.decision.value,
            location,
        )

    async def refuse_command(
        self,
        payload: dict[str, Any],
        *,
        rule: str = "",
        reason: str = "",
        verdict: PolicyVerdict | None = None,
        from_client_pump: bool = True,
    ) -> None:
        """Record a refused command and end the session (``verdict``, or a block built from rule and reason).

        Only the client pump may read the agent's socket while closing; a background task
        (``from_client_pump=False``) answers the command and closes without reading.
        """
        action = protocol_action(payload)
        if verdict is None:
            verdict = PolicyVerdict.block(reason=reason, rule=rule, evidence={"method": action.method})
        context, location = await self._record_command(action, verdict)
        logger.warning(
            "Refused CDP command session=%s method=%s rule=%s at %s",
            self.session_id,
            action.method,
            verdict.rule,
            location,
        )
        if from_client_pump:
            await self.reporter.reject(action, context, verdict, CLOSE_CODE_PRE_CONDITION)
        else:
            await self.reporter.terminate(context, verdict, CLOSE_CODE_PRE_CONDITION, answer=action)

    async def _record_command(self, action: CdpAction, verdict: PolicyVerdict) -> tuple[ActionContext, str]:
        """Capture the command's tab and write its record. Returns the context and the record's location."""
        async with self.writer.slot() as slot:
            state = await self.capture(action)
            context = self._context(slot.sequence, action, state)
            location = await slot.write(build_action_record(context, verdict))
        return context, location

    # Events the page starts (downloads) -----------------------------------------------

    async def govern_page_action(
        self,
        action: CdpAction,
        target_id: str | None,
        decide: Callable[[ActionContext], Awaitable[PolicyVerdict]],
    ) -> tuple[PolicyVerdict, str | None]:
        """Capture, decide, record; end the session when blocked. Returns (verdict, page URL)."""
        async with self.writer.slot() as slot:
            state = await self.capture(action, target_id)
            self.memory.update(state, "action", slot.sequence)
            context = self._context(slot.sequence, action, state)
            verdict = await decide(context)
            if verdict.blocked and not self.reporter.terminating:
                await self.reporter.terminate(context, verdict, CLOSE_CODE_PRE_CONDITION)
            location = await slot.write(build_action_record(context, verdict))
        logger.info(
            "Governed %s %s session=%s verdict=%s at %s",
            action.method,
            action.params.get("phase") or "",
            self.session_id,
            verdict.decision.value,
            location,
        )
        return verdict, state.url

    # Page guard callbacks -----------------------------------------------------------

    async def on_guard_violation(self, target_id: str, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or SystemRule.SYNTHETIC_EVENT.value)
        if self.reporter.terminating:
            return
        try:
            async with self.writer.slot() as slot:
                if self.reporter.terminating:
                    return
                action = CdpAction(
                    message_id=None,
                    method=violation_method(kind),
                    kind=ActionKind.SYNTHETIC,
                    params=event,
                )
                # End the session first, then capture and record.
                placeholder = self._context(slot.sequence, action, BrowserState())
                verdict = PolicyVerdict.block(reason=violation_reason(event), rule=kind, evidence={"event": event})
                await self.reporter.terminate(placeholder, verdict, CLOSE_CODE_PRE_CONDITION)
                state = await self.capture(action, target_id)
                context = self._context(slot.sequence, action, state)
                location = await slot.write(build_action_record(context, verdict))
            logger.warning("Statelock guard violation rule=%s session=%s at %s", kind, self.session_id, location)
        except Exception:
            logger.exception("Failed to handle guard violation for session %s", self.session_id)

    async def on_request_record(self, record: dict[str, Any]) -> None:
        line = self.scrubber.data(build_network_record(record))
        await self.sink.append_network_record(self.session_id, line)
        await self.events.emit(event_names.REQUEST_RECORDED, {"session_id": self.session_id, **line})
