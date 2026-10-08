# SPDX-License-Identifier: Apache-2.0
"""How policy text values are compared with page text and element labels.

Pages can render the same words with different code points: a non-breaking
space, a line break, a soft hyphen or zero-width space, full-width letters, or
different case. Every text rule compares the normalized forms, so a value in a
policy matches however the page spells it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator

if TYPE_CHECKING:
    from statelock.core.state import TargetElement


def normalize_text(value: str, *, casefold: bool = True) -> str:
    """NFKC and case folding; invisible format characters (soft hyphen, zero-width
    space, ...) removed; runs of whitespace collapsed to one space.

    ``casefold=False`` keeps case, for readers that need it (currency codes in amounts).
    """
    text = unicodedata.normalize("NFKC", value)
    if casefold:
        # NFKC again after casefold: folding can produce characters NFKC would change.
        text = unicodedata.normalize("NFKC", text.casefold())
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    return " ".join(text.split())


def compact_text(value: str) -> str:
    """normalize_text without any whitespace."""
    return "".join(normalize_text(value).split())


def comparable_text(value: str) -> str:
    """compact_text without punctuation, for comparing field values.

    Punctuation between two digits ("12/05", "1.5") and a leading minus ("-5") are kept,
    so differently formatted numbers stay different. Letters of every script are kept.
    """
    text = compact_text(value)
    kept: list[str] = []
    for index, char in enumerate(text):
        if unicodedata.category(char)[0] == "P":
            between_digits = 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit()
            leading_sign = index == 0 and char == "-" and len(text) > 1 and text[1].isdigit()
            if not (between_digits or leading_sign):
                continue
        kept.append(char)
    return "".join(kept)


def _visible_text(value: str) -> str:
    # Empty once normalized ("", " ", a soft hyphen), a value is contained in every page and label.
    if not compact_text(value):
        raise ValueError("text values must contain visible characters")
    return value


# A policy text value (rule values, trigger click_text).
NonEmptyText = Annotated[str, AfterValidator(_visible_text)]


def unreadable_label(target: TargetElement) -> bool:
    """An element in a frame Statelock cannot read, or an interactive one with no label:
    any click text might be on it."""
    return target.unresolved or (target.is_interactive and not normalize_text(target.label_text))


def matched_value(target: TargetElement, values: Iterable[str]) -> str | None:
    """The first value contained in the element's text or aria-label (normalized), or None."""
    label = normalize_text(target.label_text)
    return next((value for value in values if normalize_text(value) in label), None)


def label_matches(target: TargetElement, values: Iterable[str]) -> bool:
    """Whether click text names the element. An unreadable label matches (fail closed)."""
    return unreadable_label(target) or matched_value(target, values) is not None
