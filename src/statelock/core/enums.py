# SPDX-License-Identifier: Apache-2.0
"""Shared enumerations. Values are part of the artifact and wire formats."""

from __future__ import annotations

from enum import Enum


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    # A rule with on_fail: review failed; a human decides (see statelock.review).
    REVIEW = "review"


class ViolationType(str, Enum):
    PRE_CONDITION = "pre_condition"
    POST_CONDITION = "post_condition"


class SystemRule(str, Enum):
    """Rules enforced by Statelock itself, independent of policy files."""

    AGENT_REGISTRATION = "agent_registration"
    STATE_CAPTURE = "state_capture"
    TARGET_SETUP = "target_setup"  # page guard install or download configuration failed
    WRAPPED_COMMAND = "wrapped_command"
    SCRIPT_MODIFICATION = "script_modification"
    JAVASCRIPT_URL = "javascript_url"
    NAVIGATION_URL = "navigation_url"  # a URL other than http(s) or about:blank loaded by the agent
    UNGOVERNED_COMMAND = "ungoverned_command"  # a command that acts outside governance (e.g. exposeDevToolsProtocol)
    STATELOCK_INTERNALS = "statelock_internals"  # an agent command naming or targeting Statelock's own worlds
    SYNTHETIC_EVENT = "synthetic_event"
    AGENT_CODE_REQUEST = "agent_code_request"
    UNTRUSTED_FORM_SUBMISSION = "untrusted_form_submission"
    UNTRUSTED_FILE_INPUT = "untrusted_file_input"
    AGENT_NETWORK_INTERCEPTION = "agent_network_interception"
    FILE_UPLOAD_PATH = "file_upload_path"
    DOWNLOAD_LIMIT = "download_limit"
    COOKIE_EXPORT = "cookie_export"  # the agent may not read the browser's cookies
    REQUEST_ACCESS = "request_access"  # a Statelock.fetch the agent's request_access does not allow
    REQUEST_CONCURRENCY = "request_concurrency"  # a Statelock.fetch beyond the session's concurrent-request limit
    SECRET_INJECTION = "secret_injection"  # noqa: S105 - a rule name: a {{secret:...}} placeholder where it may not be typed


class TargetSelection(str, Enum):
    ACTION_SESSION = "action_session"
    HEURISTIC = "heuristic"


class InitiatedBy(str, Enum):
    AGENT_CODE = "agent_code"
    AGENT_NAVIGATION = "agent_navigation"
    SITE = "site"
    # A request Statelock made for the agent (Statelock.fetch, policy request_access).
    STATELOCK_REQUEST = "statelock_request"
    UNKNOWN = "unknown"
