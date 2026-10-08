# SPDX-License-Identifier: Apache-2.0
"""Shared domain types. Other Statelock modules depend on core, never the reverse."""

from statelock.core.actions import DOWNLOAD_METHOD, FILE_UPLOAD_METHOD, ActionKind, CdpAction
from statelock.core.enums import Decision, InitiatedBy, SystemRule, TargetSelection, ViolationType
from statelock.core.state import ActionContext, BrowserState, TargetElement
from statelock.core.verdict import PolicyVerdict

__all__ = [
    "DOWNLOAD_METHOD",
    "FILE_UPLOAD_METHOD",
    "ActionContext",
    "ActionKind",
    "BrowserState",
    "CdpAction",
    "Decision",
    "InitiatedBy",
    "PolicyVerdict",
    "SystemRule",
    "TargetElement",
    "TargetSelection",
    "ViolationType",
]
