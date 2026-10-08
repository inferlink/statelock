# SPDX-License-Identifier: Apache-2.0
"""Policy rules and the rule registry.

A policy entry in YAML names exactly one rule, and optionally a ``trigger``
(statelock.policy.triggers):

    pre_conditions:
      - prohibit_click_text:
          values: [Delete]

Each rule is a Pydantic model with an async ``check``. Custom rules are added
with ``@register_rule``, from a package's ``statelock.rules`` entry point or a
module in ``STATELOCK_RULE_MODULES`` (statelock.policy.extensions). A rule that
raises or takes longer than its ``check_timeout`` fails: the action is blocked.

Every pre-condition rule takes ``on_fail``: ``block`` (default) ends the session;
``review`` pauses the action for a human reviewer (see statelock.review).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from statelock.core.actions import (
    DOWNLOAD_METHOD,
    FILE_UPLOAD_METHOD,
    KEY_METHOD,
    ActionKind,
    is_activation_key,
    is_pointer_method,
    starts_activation,
)
from statelock.core.state import ActionContext, BrowserState, normalized_label
from statelock.policy.fields import REMEMBERED_PREFIX, parse_number
from statelock.policy.patterns import checked_regex
from statelock.policy.perception import ExtractType, PerceptionEvaluator, PerceptionRequest
from statelock.policy.triggers import ActionTrigger

Phase = Literal["pre", "post"]
OnFail = Literal["block", "review"]
DEFAULT_CHECK_TIMEOUT = 30.0
TRIGGER_KEY = "trigger"
# An empty text would be contained in every page and every label.
NonEmptyText = Annotated[str, Field(min_length=1)]
FieldRef = Annotated[str, Field(pattern=r"^(?:remembered\.)?[A-Za-z_][A-Za-z0-9_]*$")]


@dataclass
class RuleContext:
    action: ActionContext
    # The state the rule checks: pre-action state for pre-conditions,
    # post-action state for post-conditions.
    state: BrowserState | None
    phase: Phase
    perception: PerceptionEvaluator

    @property
    def is_pointer(self) -> bool:
        """Any pointer action: mouse, tap gesture, drag and drop."""
        return is_pointer_method(self.action.method)

    @property
    def is_activation(self) -> bool:
        """A press, tap, drop, upload, or Enter/Space/shortcut key down (core.actions.starts_activation).
        An action of an unknown kind counts as one, so checks limited to activations still run."""
        if self.action.action_kind not in {kind.value for kind in ActionKind}:
            return True
        return starts_activation(self.action.method, self.action.params)

    @property
    def is_key_activation(self) -> bool:
        return self.action.method == KEY_METHOD and is_activation_key(self.action.params)

    def field(self, ref: str) -> str | None:
        """A field value: ``name`` on the checked page, or ``remembered.<name>`` from the session."""
        if ref.startswith(REMEMBERED_PREFIX):
            entry = self.action.remembered.get(ref[len(REMEMBERED_PREFIX) :])
            raw = entry.get("value") if isinstance(entry, dict) else None
        else:
            raw = (self.state.extracted_fields if self.state else {}).get(ref)
        text = str(raw).strip() if raw is not None else ""
        return text or None

    def field_evidence(self, *refs: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {"fields": {ref: self.field(ref) for ref in refs}}
        remembered = {
            ref: self.action.remembered.get(ref[len(REMEMBERED_PREFIX) :])
            for ref in refs
            if ref.startswith(REMEMBERED_PREFIX)
        }
        if remembered:
            evidence["remembered"] = remembered
        return evidence


@dataclass
class RuleFailure:
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_name: ClassVar[str]
    phases: ClassVar[frozenset[str]] = frozenset({"pre", "post"})
    # Also run when a download completes (with the size and SHA-256 known).
    checked_on_download_completion: ClassVar[bool] = False
    # Whether on_fail: review is allowed (downloads cannot be paused for review).
    reviewable: ClassVar[bool] = True
    # Whether the entry may take a trigger. Rules that check their own kind of action
    # (uploads, downloads) never see a triggering activation.
    triggerable: ClassVar[bool] = True
    # Seconds check() may take; then the rule fails (None: the rule bounds itself).
    check_timeout: ClassVar[float | None] = DEFAULT_CHECK_TIMEOUT

    on_fail: OnFail = "block"
    # Set from the entry's ``trigger`` key: only these actions are checked.
    trigger: ActionTrigger | None = None

    @property
    def needs_review(self) -> bool:
        return self.on_fail == "review"

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        raise NotImplementedError


RULES: dict[str, type[Rule]] = {}


def register_rule(cls: type[Rule]) -> type[Rule]:
    """Add a rule to the registry. A second, different class for a name already taken is refused."""
    existing = RULES.get(cls.rule_name)
    # A re-import of the same module (same module and class name) is fine.
    if existing is not None and (existing.__module__, existing.__qualname__) != (cls.__module__, cls.__qualname__):
        raise ValueError(f"rule {cls.rule_name} is already registered by {existing.__module__}.{existing.__qualname__}")
    RULES[cls.rule_name] = cls
    return cls


def parse_rule(entry: Any, phase: Phase) -> Rule:
    """Build a rule from a one-key mapping such as {"require_page_text": {...}}."""
    if isinstance(entry, Rule):
        return entry
    if not isinstance(entry, dict):
        # ValueError, not TypeError: Pydantic reports only ValueError as a validation error.
        raise ValueError(f"policy rule must be a mapping, got {type(entry).__name__}")  # noqa: TRY004
    names = [key for key in entry if key in RULES]
    unknown = [key for key in entry if key not in RULES and key != TRIGGER_KEY]
    if unknown:
        raise ValueError(
            f"unknown rule(s): {', '.join(unknown)}; known: {', '.join(sorted(RULES))}. "
            "A custom rule must be loaded first (STATELOCK_RULE_MODULES or a statelock.rules entry point)."
        )
    if len(names) != 1:
        raise ValueError(f"each policy entry must name exactly one rule, got {len(names)}")
    name = names[0]
    rule_cls = RULES[name]
    if phase not in rule_cls.phases:
        raise ValueError(f"rule {name} is not allowed in {phase}-conditions")
    rule = rule_cls.model_validate(_rule_options(entry, name, rule_cls))
    if rule.needs_review and phase == "post":
        raise ValueError(f"rule {name}: on_fail: review is not allowed in post-conditions (the action already ran)")
    if rule.needs_review and not rule_cls.reviewable:
        raise ValueError(f"rule {name}: on_fail: review is not supported")
    return rule


def _rule_options(entry: dict[str, Any], name: str, rule_cls: type[Rule]) -> dict[str, Any]:
    """The rule's options, with the entry's trigger. A trigger is only accepted beside the rule."""
    raw_options = entry[name] or {}
    if not isinstance(raw_options, dict):
        raise ValueError(f"rule {name}: options must be a mapping")  # noqa: TRY004 - Pydantic needs ValueError
    if TRIGGER_KEY in raw_options:
        raise ValueError(f"rule {name}: trigger belongs beside the rule in the entry, not in its options")
    options = dict(raw_options)
    if TRIGGER_KEY in entry:
        if not rule_cls.triggerable:
            raise ValueError(f"rule {name} does not take a trigger: it checks its own kind of action")
        options[TRIGGER_KEY] = entry[TRIGGER_KEY]
    return options


@register_rule
class ProhibitClickText(Rule):
    """Block clicks (and Enter/Space activation) on elements whose text matches a value."""

    rule_name: ClassVar[str] = "prohibit_click_text"
    phases: ClassVar[frozenset[str]] = frozenset({"pre"})

    values: list[NonEmptyText] = Field(min_length=1)

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        # Only what activates the element: hovering over or scrolling past it is not a click.
        if not (ctx.is_pointer and ctx.is_activation) and not ctx.is_key_activation:
            return None
        target = ctx.state.target_element if ctx.state else None
        if target is None:
            if ctx.is_key_activation:
                return None  # no focused element: the key press activates nothing
            return RuleFailure(
                "Blocked click because the click target could not be resolved, "
                "so prohibited click text could not be verified."
            )
        text = target.normalized_label
        if not text and target.is_interactive:
            return RuleFailure("Blocked activation because the target has no readable label")
        action_name = "click" if ctx.is_pointer else "key activation"
        for value in self.values:
            if normalized_label(value) in text:
                return RuleFailure(
                    f"Blocked {action_name} because target element text matched prohibited value: {value}"
                )
        return None


@register_rule
class RequirePageText(Rule):
    rule_name: ClassVar[str] = "require_page_text"

    values: list[NonEmptyText] = Field(min_length=1)

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        page_text = (ctx.state.page_text or "").casefold() if ctx.state else ""
        for value in self.values:
            if value.casefold() not in page_text:
                return RuleFailure(f"Blocked action because required page text was missing: {value}")
        return None


@register_rule
class ProhibitPageText(Rule):
    """The page text must not contain any listed value (e.g. a login form after logging in)."""

    rule_name: ClassVar[str] = "prohibit_page_text"

    values: list[NonEmptyText] = Field(min_length=1)

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        if ctx.state is None:
            return RuleFailure("Blocked action because the page text could not be checked (no page state)")
        page_text = (ctx.state.page_text or "").casefold()
        for value in self.values:
            if value.casefold() in page_text:
                return RuleFailure(f"Blocked action because the page shows prohibited text: {value}")
        if ctx.state.page_text_truncated:
            return RuleFailure("Blocked action because prohibited text could not be checked on the whole page")
        return None


def values_equal(left: str, right: str) -> bool:
    """Equal as numbers when both parse ("5000" == "$5,000.00"), otherwise as normalized
    text (see normalize_comparable_value). When both sides show a currency symbol, the
    symbols must be the same ("€5" != "£5")."""
    left_currency, right_currency = currency_symbols(left), currency_symbols(right)
    if left_currency and right_currency and left_currency != right_currency:
        return False
    left_codes, right_codes = currency_codes(left), currency_codes(right)
    if left_codes and right_codes and left_codes != right_codes:
        return False
    left_number, right_number = parse_number(left), parse_number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    left_text, right_text = normalize_comparable_value(left), normalize_comparable_value(right)
    if not left_text or not right_text:
        # Two different texts never become equal by losing all their characters ("!" == "?").
        return not left.strip() and not right.strip()
    return left_text == right_text


def currency_symbols(value: str) -> set[str]:
    return {char for char in unicodedata.normalize("NFKC", value) if unicodedata.category(char) == "Sc"}


def currency_codes(value: str) -> set[str]:
    return set(re.findall(r"\b(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY|INR)\b", value.upper()))


def normalize_comparable_value(value: str) -> str:
    """NFKC and casefold; whitespace, invisible format characters and punctuation removed.
    Punctuation between two digits ("12/05", "1.5") and a leading sign ("-5") are kept,
    so differently formatted numbers stay different. Letters of every script are kept."""
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    kept: list[str] = []
    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if char.isspace() or category[0] == "Z" or category == "Cf":
            continue
        if category[0] == "P":
            between_digits = 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit()
            leading_sign = index == 0 and char == "-" and len(text) > 1 and text[1].isdigit()
            if not (between_digits or leading_sign):
                continue
        kept.append(char)
    return "".join(kept)


@register_rule
class AssertFieldEqual(Rule):
    """A field equals another field (``right``) or a constant (``value``): as numbers when
    both parse, otherwise as text ignoring case, spaces and punctuation."""

    rule_name: ClassVar[str] = "assert_field_equal"

    left: FieldRef
    right: FieldRef | None = None
    value: str | int | float | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _not_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):  # YAML yes/no: almost always a mistake
            raise ValueError("value must be a string or a number, not a boolean")  # noqa: TRY004
        return value

    @model_validator(mode="after")
    def _one_right_side(self) -> AssertFieldEqual:
        if (self.right is None) == (self.value is None):
            raise ValueError("assert_field_equal needs exactly one of right or value")
        return self

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        refs = (self.left,) if self.right is None else (self.left, self.right)
        evidence = ctx.field_evidence(*refs)
        left = ctx.field(self.left)
        right = ctx.field(self.right) if self.right is not None else str(self.value)
        label = self.right if self.right is not None else "the required value"
        if left is None or right is None:
            missing = ", ".join(ref for ref, found in ((self.left, left), (self.right, right)) if ref and found is None)
            return RuleFailure(f"Blocked action because required fields were missing: {missing}", evidence)
        if not values_equal(left, right):
            return RuleFailure(f"Blocked action because {self.left} ({left}) did not equal {label} ({right})", evidence)
        return None


COMPARE_OPERATORS: dict[str, Any] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


@register_rule
class AssertCompare(Rule):
    """Numeric comparison of a field with another field or a constant.

        - assert_compare: {left: erp_invoice_amount, op: "<=", value: 10000}
        - assert_compare: {left: remembered.bank_deposit, op: ">=", right: erp_invoice_amount}

    Amounts such as "$5,000.00" are parsed; a missing or unparseable value blocks.
    """

    rule_name: ClassVar[str] = "assert_compare"

    left: FieldRef
    op: Literal["<", "<=", ">", ">=", "==", "!="]
    right: FieldRef | None = None
    value: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def _one_right_side(self) -> AssertCompare:
        if (self.right is None) == (self.value is None):
            raise ValueError("assert_compare: set exactly one of right or value")
        return self

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        refs = [self.left] if self.right is None else [self.left, self.right]
        evidence = ctx.field_evidence(*refs)
        left_text = ctx.field(self.left)
        right_text = ctx.field(self.right) if self.right is not None else None
        right_label = self.right if self.right is not None else _format_number(self.value)
        left_number = parse_number(left_text) if left_text is not None else None
        right_number = self.value if self.right is None else (parse_number(right_text) if right_text else None)
        if left_number is None or right_number is None:
            return RuleFailure(
                f"Blocked action because {self.left} {self.op} {right_label} could not be checked: "
                f"missing or non-numeric value ({self.left}={left_text!r}"
                + (f", {self.right}={right_text!r})" if self.right is not None else ")"),
                evidence,
            )
        if not COMPARE_OPERATORS[self.op](left_number, right_number):
            shown_right = f"{self.right} ({right_text})" if self.right is not None else right_label
            return RuleFailure(
                f"Blocked action because {self.left} ({left_text}) {self.op} {shown_right} is false", evidence
            )
        return None


def _format_number(value: float | None) -> str:
    if value is None:
        return "None"
    return f"{value:,.0f}" if value.is_integer() else f"{value:,}"


class FileRestriction(Rule):
    """Shared checks of restrict_uploads and restrict_downloads: file name, extension and size."""

    phases: ClassVar[frozenset[str]] = frozenset({"pre"})
    triggerable: ClassVar[bool] = False

    extensions: list[str] = Field(default_factory=list)
    max_bytes: int | None = Field(default=None, ge=0)
    name_pattern: str | None = None

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        return [ext.casefold() if ext.startswith(".") else f".{ext.casefold()}" for ext in value]

    @field_validator("name_pattern")
    @classmethod
    def _compile_name(cls, value: str | None) -> str | None:
        return checked_regex(value, "name_pattern")

    def check_name(self, name: str) -> str | None:
        if self.extensions and not any(name.casefold().endswith(ext) for ext in self.extensions):
            return f"{name} does not have an allowed extension ({', '.join(self.extensions)})"
        if self.name_pattern is not None and re.search(self.name_pattern, name) is None:
            return f"{name} does not match {self.name_pattern}"
        return None

    def check_size(self, name: str, size: Any) -> str | None:
        """A missing or non-integer size fails (fail closed)."""
        if self.max_bytes is not None and (not isinstance(size, int) or size > self.max_bytes):
            return f"{name} is {size} bytes; the limit is {self.max_bytes}"
        return None


@register_rule
class RestrictUploads(FileRestriction):
    """Limit governed file uploads (DOM.setFileInputFiles). Other actions pass.

        - restrict_uploads: {extensions: [.pdf, .csv], max_bytes: 10000000, max_files: 1,
                             name_pattern: "^INV-[0-9]+"}

    ``max_files`` limits the files in one upload. Checks the evidence Statelock
    recorded for each uploaded file; an upload without that evidence blocks.
    """

    rule_name: ClassVar[str] = "restrict_uploads"

    max_files: int | None = Field(default=None, ge=0)

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        if ctx.action.method != FILE_UPLOAD_METHOD:
            return None
        uploads = ctx.action.params.get("statelock_uploads")
        if not isinstance(uploads, list):
            return RuleFailure("Blocked upload because Statelock has no evidence for the files.")
        evidence = {"uploads": uploads}
        if self.max_files is not None and len(uploads) > self.max_files:
            return RuleFailure(f"Blocked upload of {len(uploads)} files; at most {self.max_files} allowed", evidence)
        for upload in uploads:
            item = upload if isinstance(upload, dict) else {}
            name = str(item.get("name") or "")
            failure = self.check_name(name) or self.check_size(name, item.get("size"))
            if failure is not None:
                return RuleFailure(f"Blocked upload: {failure}", evidence)
        return None


@register_rule
class RestrictDownloads(FileRestriction):
    """Limit downloads the page starts. Other actions pass.

        - restrict_downloads: {extensions: [.pdf], max_bytes: 20000000, max_downloads: 5,
                               name_pattern: "^report", url_pattern: "^https://portal\\.example\\.com/"}

    Name, extension, source URL and ``max_downloads`` (per session) are checked
    when the download begins; ``max_bytes`` when it completes. A blocked download
    is cancelled or deleted, never handed to the agent.
    """

    rule_name: ClassVar[str] = "restrict_downloads"
    checked_on_download_completion: ClassVar[bool] = True
    reviewable: ClassVar[bool] = False

    max_downloads: int | None = Field(default=None, ge=0)
    url_pattern: str | None = None

    @field_validator("url_pattern")
    @classmethod
    def _compile_url(cls, value: str | None) -> str | None:
        return checked_regex(value, "url_pattern")

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        if ctx.action.method != DOWNLOAD_METHOD:
            return None
        params = ctx.action.params
        failure = self._check(params)
        return RuleFailure(f"Blocked download: {failure}", {"download": params}) if failure else None

    def _check(self, params: dict[str, Any]) -> str | None:
        name = str(params.get("suggested_filename") or "")
        url = str(params.get("url") or "")
        count = params.get("download_count")
        if params.get("phase") == "complete":
            return self.check_name(name) or self.check_size(name, params.get("size"))
        if failure := self.check_name(name):
            return failure
        if self.url_pattern is not None and re.search(self.url_pattern, url) is None:
            return f"{url} does not match {self.url_pattern}"
        if self.max_downloads is not None:
            if not isinstance(count, int) or isinstance(count, bool):
                return (
                    f"the download count is unknown ({count!r}); max_downloads {self.max_downloads} cannot be checked"
                )
            if count > self.max_downloads:
                return f"download {count} exceeds max_downloads {self.max_downloads}"
        return None


class CrossCheck(BaseModel):
    """A value the model read must equal a page field or remembered value (deterministic)."""

    model_config = ConfigDict(extra="forbid")

    extracted: str
    field: FieldRef


@register_rule
class VisualAssert(Rule):
    """A vision-language model checks the screenshot (see statelock.policy.perception).

    - visual_assert:
        check_name: deposit_matches_invoice
        instruction: "The page shows a bank deposit and an ERP invoice with the same amount."
        extract: {invoice_amount: string}              # values the model reads off the screenshot
        cross_check:                                   # optional: the model's reading must match the DOM
          - {extracted: invoice_amount, field: erp_invoice_amount}
        # when: activation                             # default: only clicks, taps, Enter... ; or: all
        on_fail: review

    Fails closed: no evaluator, no screenshot, timeout or invalid output block (or go to review).
    """

    rule_name: ClassVar[str] = "visual_assert"
    # The evaluator has its own per-call timeout and retries (STATELOCK_PERCEPTION_*).
    check_timeout: ClassVar[float | None] = None

    check_name: str
    instruction: str
    extract: dict[str, ExtractType] = Field(default_factory=dict)
    cross_check: list[CrossCheck] = Field(default_factory=list)
    # A model call per action would be slow and costly: by default only activations
    # (press, tap, drop, upload, Enter/Space/shortcuts) are checked in pre-conditions.
    when: Literal["activation", "all"] = "activation"

    @model_validator(mode="after")
    def _cross_checks_extract(self) -> VisualAssert:
        missing = sorted({c.extracted for c in self.cross_check} - set(self.extract))
        if missing:
            raise ValueError(f"cross_check reads values that extract does not ask for: {', '.join(missing)}")
        return self

    def applies(self, ctx: RuleContext) -> bool:
        return ctx.phase == "post" or self.when == "all" or ctx.is_activation

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        if not self.applies(ctx):
            return None
        verdict = await ctx.perception.evaluate(
            PerceptionRequest(
                check_name=self.check_name,
                instruction=self.instruction,
                phase=ctx.phase,
                context=ctx.action,
                state=ctx.state,
                extract=dict(self.extract),
            )
        )
        evidence: dict[str, Any] = {"perception": verdict.model_dump(), "check_name": self.check_name}
        if not verdict.passed:
            return RuleFailure(f"Visual check {self.check_name} failed: {verdict.reason}", evidence)
        for check in self.cross_check:
            seen = verdict.extracted.get(check.extracted)
            expected = ctx.field(check.field)
            evidence.setdefault("cross_check", []).append(
                {"extracted": check.extracted, "seen": seen, "field": check.field, "value": expected}
            )
            if seen is None or expected is None:
                return RuleFailure(
                    f"Visual check {self.check_name}: {check.extracted} or {check.field} is missing", evidence
                )
            if not values_equal(str(seen), expected):
                return RuleFailure(
                    f"Visual check {self.check_name}: the model read {check.extracted}={seen!r}, "
                    f"but {check.field} is {expected!r}",
                    evidence,
                )
        return None
