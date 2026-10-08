# SPDX-License-Identifier: Apache-2.0
"""What install() (async) and install_sync() share: replacing Playwright methods,
and the default timeouts the governed ``expect_download`` honours."""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from typing import Any

Patch = tuple[type, str, Any]

# Timeouts set with Page/BrowserContext.set_default_timeout while installed (ms).
# Playwright has no public getter, so the patched setters record them here.
_default_timeouts: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()


class Patches:
    """Replace Playwright methods, keeping the originals (install is idempotent)."""

    def __init__(self, patches: Sequence[Patch]) -> None:
        self._patches = patches
        self._originals: dict[tuple[type, str], Any] = {}

    def original(self, cls: type, name: str) -> Any:
        return self._originals[(cls, name)]

    def install(self) -> None:
        for cls, name, replacement in self._patches:
            if (cls, name) not in self._originals:
                self._originals[(cls, name)] = getattr(cls, name)
                setattr(cls, name, replacement)

    def uninstall(self) -> None:
        for (cls, name), original in list(self._originals.items()):
            setattr(cls, name, original)
            del self._originals[(cls, name)]


def record_default_timeout(owner: Any, timeout: float) -> None:
    """Called by the patched set_default_timeout of a page or browser context."""
    _default_timeouts[owner] = float(timeout)


def default_timeout_ms(page: Any) -> float | None:
    """The page's default timeout, else its context's (as Playwright resolves it), or None."""
    for owner in (page, page.context):
        if owner in _default_timeouts:
            return _default_timeouts[owner]
    return None
