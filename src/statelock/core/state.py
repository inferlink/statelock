# SPDX-License-Identifier: Apache-2.0
"""Captured browser state and the per-action context."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from statelock.core.actions import ActionKind, CdpAction
from statelock.core.enums import TargetSelection

SECRET_AUTOCOMPLETE = {"current-password", "new-password", "one-time-code", "cc-number", "cc-csc", "cc-exp"}


class TargetElement(BaseModel):
    """The element that receives an action: under the pointer, or focused.

    ``unresolved``: the action goes into a frame Statelock cannot read (out of process or
    cross-origin); the element described is the frame's owner (e.g. the IFRAME). Its label
    is unknown, so it counts as an interactive element with no readable label, which
    click-text rules block and click-text triggers match.
    """

    model_config = ConfigDict(extra="allow")

    source: str | None = None  # "pointer" or "focus"
    tag_name: str | None = None
    id: str | None = None
    class_name: str | None = None
    text: str | None = None
    aria_label: str | None = None
    role: str | None = None
    href: str | None = None
    input_type: str | None = None  # for <input>: its type (text, password, ...)
    autocomplete: str | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    unresolved: bool = False
    frame_url: str | None = None  # the unresolved frame's src

    @property
    def is_secret_input(self) -> bool:
        """A password, one-time-code or card field: what is typed into it is a secret."""
        tokens = set((self.autocomplete or "").casefold().split())
        return self.input_type == "password" or bool(tokens & SECRET_AUTOCOMPLETE)

    @property
    def label_text(self) -> str:
        """Visible text and aria-label, used for text-matching rules (empty when unresolved)."""
        if self.unresolved:
            return ""
        return " ".join(part.strip() for part in (self.text or "", self.aria_label or "") if part and part.strip())

    @property
    def is_interactive(self) -> bool:
        return (
            self.unresolved
            or (self.tag_name or "").upper() in {"BUTTON", "A", "SUMMARY"}
            or ((self.tag_name or "").upper() == "INPUT" and self.input_type in {"button", "submit", "reset", "image"})
            or (self.role or "").casefold() in {"button", "link", "menuitem"}
        )


class BrowserState(BaseModel):
    url: str | None = None
    title: str | None = None
    target_id: str | None = None
    target_type: str | None = None
    target_selection: TargetSelection | None = None
    target_element: TargetElement | None = None
    page_text: str | None = None
    page_text_truncated: bool = False
    # Shown parts of the page whose text could not be read (cross-origin frames, by src).
    page_text_unread: list[str] = Field(default_factory=list)
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    accessibility_tree: dict[str, Any] = Field(default_factory=dict)
    viewport: dict[str, Any] = Field(default_factory=dict)
    dom_snapshot: dict[str, Any] = Field(default_factory=dict)
    screenshot_base64: str | None = None
    capture_error: str | None = None


class ActionContext(BaseModel):
    session_id: str
    agent_id: str | None = None
    # Tenant of the authenticated agent (None when authentication is off).
    tenant_id: str | None = None
    sequence: int
    method: str
    action_kind: ActionKind
    params: dict[str, Any] = Field(default_factory=dict)
    cdp_message_id: int | None = None
    cdp_session_id: str | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    browser_state: BrowserState | None = None
    post_browser_state: BrowserState | None = None
    # Values remembered earlier in the session (policy fields with remember: true),
    # as {name: {value, url, source, sequence, captured_at}}.
    remembered: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_action(
        cls,
        *,
        session_id: str,
        sequence: int,
        action: CdpAction,
        browser_state: BrowserState | None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> ActionContext:
        return cls(
            session_id=session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            sequence=sequence,
            method=action.method,
            action_kind=action.kind,
            params=action.params,
            cdp_message_id=action.message_id,
            cdp_session_id=action.session_id,
            browser_state=browser_state,
        )

    def summary(self, state: BrowserState | None = None) -> dict[str, Any]:
        """Compact description used as verdict evidence."""
        state = state if state is not None else self.browser_state
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "sequence": self.sequence,
            "method": self.method,
            "action_kind": self.action_kind.value,
            "params": self.params,
            "browser_url": state.url if state else None,
            "browser_title": state.title if state else None,
            "target_element": (
                state.target_element.model_dump(exclude_none=True) if state and state.target_element else None
            ),
            "extracted_fields": state.extracted_fields if state else {},
            "has_screenshot": bool(state and state.screenshot_base64),
            "capture_error": state.capture_error if state else None,
        }
