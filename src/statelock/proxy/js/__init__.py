# SPDX-License-Identifier: Apache-2.0
"""JavaScript sources injected into pages."""

from __future__ import annotations

from functools import cache
from importlib.resources import files

# How many nested frames (and shadow roots) Statelock follows, in Python and in the scripts
# (``__STATELOCK_MAX_FRAME_DEPTH__``). Deeper nesting fails closed: a frame is governed, a
# target element is unresolved.
MAX_FRAME_DEPTH = 32


@cache
def load_js(name: str) -> str:
    source = files(__package__).joinpath(name).read_text(encoding="utf-8")
    return source.replace("__STATELOCK_MAX_FRAME_DEPTH__", str(MAX_FRAME_DEPTH))
