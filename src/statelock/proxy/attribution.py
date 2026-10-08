# SPDX-License-Identifier: Apache-2.0
"""Tag the agent's page code so the browser can report what it started.

The proxy appends a sourceURL comment to every piece of code the agent sends
(Runtime.evaluate, Runtime.callFunctionOn, ...). Stack traces then show
AGENT_SOURCE_URL for agent code, for scripts that agent code creates (tracked
through Debugger.scriptParsed), and through async parents.
"""

from __future__ import annotations

from typing import Any

AGENT_SOURCE_URL = "__statelock_agent__"
# Code Statelock runs for a governed request (Statelock.fetch): attributed to Statelock.
REQUEST_SOURCE_URL = "__statelock_request__"
# The trailing newline keeps Runtime.callFunctionOn declarations valid.
AGENT_SOURCE_TAG = f"\n//# sourceURL={AGENT_SOURCE_URL}\n"

_TAGGED_FIELDS = {
    "Runtime.evaluate": "expression",
    "Runtime.callFunctionOn": "functionDeclaration",
    "Runtime.compileScript": "expression",
    "Page.addScriptToEvaluateOnNewDocument": "source",
    "Page.reload": "scriptToEvaluateOnLoad",
    "Debugger.evaluateOnCallFrame": "expression",
}


def tag_agent_code(payload: dict[str, Any]) -> bool:
    """Tag code in an agent CDP command. Returns True if the payload changed.

    The last sourceURL in a script wins, so the tag overrides any sourceURL the
    client added.
    """
    method = payload.get("method")
    params = payload.get("params")
    field = _TAGGED_FIELDS.get(str(method))
    if field is None or not isinstance(params, dict):
        return False
    value = params.get(field)
    if not isinstance(value, str):
        return False
    params[field] = value + AGENT_SOURCE_TAG
    if method == "Runtime.compileScript":
        params["sourceURL"] = AGENT_SOURCE_URL
    return True


def stack_frames(stack: Any, max_depth: int = 64) -> list[dict[str, Any]]:
    """All call frames of a Runtime.StackTrace, including async parents."""
    frames: list[dict[str, Any]] = []
    depth = 0
    while isinstance(stack, dict) and depth < max_depth:
        call_frames = stack.get("callFrames")
        if isinstance(call_frames, list):
            frames.extend(frame for frame in call_frames if isinstance(frame, dict))
        stack = stack.get("parent")
        depth += 1
    return frames


def frames_tainted(frames: list[dict[str, Any]], tainted_script_ids: set[str]) -> bool:
    return any(frame.get("url") == AGENT_SOURCE_URL or frame.get("scriptId") in tainted_script_ids for frame in frames)
