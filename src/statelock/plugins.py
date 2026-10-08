# SPDX-License-Identifier: Apache-2.0
"""Plugin loading. Add-ons extend Statelock through here.

A plugin is an object with a ``name`` and a ``setup(ctx)`` method, exposed as
an entry point in the ``statelock.plugins`` group:

    [project.entry-points."statelock.plugins"]
    my_plugin = "my_package.plugin:plugin"

``setup`` may replace ``ctx.services.sink`` (for example with a wrapper),
subscribe to ``ctx.services.events``, register policy rules, and add routes
or middleware to ``ctx.app``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol

from fastapi import FastAPI

from statelock.services import Services

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "statelock.plugins"


@dataclass
class PluginContext:
    app: FastAPI
    services: Services


class StatelockPlugin(Protocol):
    name: str

    def setup(self, ctx: PluginContext) -> None: ...


def discover_plugins(selection: str) -> list[StatelockPlugin]:
    """Load plugins per STATELOCK_PLUGINS: "auto", "none" or a comma-separated list of names."""
    selection = selection.strip()
    if selection.lower() == "none":
        return []
    wanted = None if selection.lower() == "auto" else {name.strip() for name in selection.split(",") if name.strip()}
    plugins: list[StatelockPlugin] = []
    found: set[str] = set()
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        if wanted is not None and entry_point.name not in wanted:
            continue
        plugins.append(entry_point.load())
        found.add(entry_point.name)
    if wanted is not None and wanted - found:
        raise RuntimeError(f"Statelock plugins not installed: {', '.join(sorted(wanted - found))}")
    return plugins


def setup_plugins(ctx: PluginContext, plugins: list[StatelockPlugin]) -> None:
    for plugin in plugins:
        plugin.setup(ctx)
        logger.info("Loaded Statelock plugin %s", plugin.name)
