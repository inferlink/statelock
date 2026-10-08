# SPDX-License-Identifier: Apache-2.0
"""JavaScript sources injected into pages."""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def load_js(name: str) -> str:
    return files(__package__).joinpath(name).read_text(encoding="utf-8")
