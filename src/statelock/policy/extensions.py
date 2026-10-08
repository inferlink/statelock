# SPDX-License-Identifier: Apache-2.0
"""Custom policy rules from outside Statelock.

A rule is a ``Rule`` subclass decorated with ``@register_rule`` (statelock.policy.rules).
Its module is loaded before the policy files, from:

- a package's ``statelock.rules`` entry point (``pip install`` is enough):

      [project.entry-points."statelock.rules"]
      my_rules = "my_package.rules"

- ``STATELOCK_RULE_MODULES``: comma-separated module names (importable) or ``.py``
  file paths, for rules that are not packaged (examples/ojs/ojs_rules.py).

Policy files then name the rule like a built-in one. Custom rules run inside the
proxy, installed by whoever runs it, never by an agent. A rule that raises or does
not answer within its ``check_timeout`` (30 s by default) blocks the action
(``check_rule`` in statelock.policy.evaluator). Keep ``check`` async; run blocking
I/O with ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import sys
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "statelock.rules"
_loaded: dict[str, ModuleType] = {}


def _load_file(path: Path) -> ModuleType:
    resolved = path.resolve()
    key = str(resolved)
    if key in _loaded:
        return _loaded[key]
    if not resolved.is_file():
        raise FileNotFoundError(f"rule module not found: {resolved}")
    name = f"statelock_rules_{resolved.stem}_{hashlib.sha256(key.encode()).hexdigest()[:8]}"
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load rule module {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _loaded[key] = module
    return module


def load_rule_modules(modules: str = "") -> list[str]:
    """Import every rule module: installed entry points, then ``modules``. Returns what was loaded."""
    loaded: list[str] = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        entry.load()
        loaded.append(f"{entry.name} ({entry.value})")
    for item in (part.strip() for part in modules.split(",")):
        if not item:
            continue
        if item.endswith(".py") or "/" in item:
            _load_file(Path(item))
        else:
            importlib.import_module(item)
        loaded.append(item)
    if loaded:
        logger.info("Loaded custom policy rules: %s", ", ".join(loaded))
    return loaded
