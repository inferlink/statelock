# SPDX-License-Identifier: Apache-2.0
"""Triggers: limit a rule to particular actions.

A pre- or post-condition entry can take a ``trigger`` beside its rule (not inside
the rule's options; restrict_uploads and restrict_downloads take none):

    pre_conditions:
      - trigger: {click_text: [Record Editorial Decision]}
        assert_compare: {left: decline_email_length, op: ">=", value: 20}
    post_conditions:
      - trigger: {click_text: [Mark as Paid]}   # or key: [Enter]
        require_page_text: {values: [Reconciliation complete]}

A triggered pre-condition runs only on an activation (a mouse or touch press, a
tap or drop, or Enter/Space) of an element whose text or aria-label contains a ``click_text``
value, or on the key down of a listed ``key``: it checks the page just before the
button does anything. A triggered post-condition runs after that action completes, on the
page it led to. Without a trigger, pre-conditions run on every action on the
policy's pages and post-conditions after every commit action.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from statelock.core.actions import KEY_DOWN_TYPES, KEY_METHOD, is_pointer_method, pressed_key_names
from statelock.core.state import ActionContext, BrowserState
from statelock.policy.text import NonEmptyText, label_matches


class ActionTrigger(BaseModel):
    """At least one of: click_text (case-insensitive substring of the element's text or
    aria-label: the element under the pointer, or the focused one for a key), key
    (key names such as "Enter")."""

    model_config = ConfigDict(extra="forbid")

    click_text: list[NonEmptyText] = Field(default_factory=list)
    key: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_one(self) -> ActionTrigger:
        if not self.click_text and not self.key:
            raise ValueError("trigger requires click_text or key")
        return self

    def matches(self, context: ActionContext, state: BrowserState | None) -> bool:
        """Whether this action is one the trigger names. ``state``: the page before the action."""
        return self._key_matches(context) or self._click_text_matches(context, state)

    def matches_before(self, context: ActionContext, state: BrowserState | None, *, is_activation: bool) -> bool:
        """For a pre-condition: the key down of a listed key, or an activation of a named element."""
        if self._key_matches(context) and context.params.get("type") in (*KEY_DOWN_TYPES, "char"):
            return True
        return is_activation and self._click_text_matches(context, state)

    def _key_matches(self, context: ActionContext) -> bool:
        if context.method != KEY_METHOD or not self.key:
            return False
        pressed = pressed_key_names(context.params)
        return any(key.casefold() in pressed for key in self.key)

    def _click_text_matches(self, context: ActionContext, state: BrowserState | None) -> bool:
        """For a click, an unresolved target counts as a match (fail closed). For a key
        press with no focused element, nothing was activated."""
        is_pointer = is_pointer_method(context.method)
        if not self.click_text or not (is_pointer or context.method == KEY_METHOD):
            return False
        target = state.target_element if state else None
        if target is None:
            return is_pointer
        return label_matches(target, self.click_text)
