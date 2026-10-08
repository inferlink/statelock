# SPDX-License-Identifier: Apache-2.0
"""Governed downloads.

Statelock, not the agent, decides where Chromium saves downloads: it sets
``Browser.setDownloadBehavior`` (allowAndName, a per-session folder, events
on) for every browser context before the agent can use it, and answers the
agent's own ``Browser.setDownloadBehavior`` / ``Page.setDownloadBehavior``
itself (a ``deny`` request is honoured). Each download is governed twice:

1. When it begins (``Browser.downloadWillBegin``): state is captured from the
   page, the policies for that page are evaluated (``restrict_downloads``
   checks name, extension, source URL and count), and the action is recorded.
2. When it completes: the file is hashed, ``restrict_downloads`` size limits
   are checked, and a second record holds name, size and SHA-256.

A blocked download is cancelled (or deleted) and ends the session, like any
violation. The file reaches the agent only after both checks pass, over the
agent's own CDP connection:

- ``Statelock.downloads`` {} -> {downloads: [{guid, name, url, state, size, sha256, reason, rule}]}
  (``rule``: for a blocked download, the policy rule that blocked it, or ``download_limit``
  for a size limit; null when allowed or when a check could not run)
- ``Statelock.downloadRead`` {guid, offset, length} -> {data (base64), eof}

The client SDK wraps this in ``statelock.client.expect_download``. Files are
deleted when the session ends.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from statelock.core.enums import SystemRule
from statelock.core.jsonutil import as_dict, as_str
from statelock.proxy.connection import CdpError
from statelock.proxy.files import ByteBudget, LocalCommandError, SessionFolder
from statelock.proxy.tasks import BackgroundTasks
from statelock.wire import DOWNLOAD_READ_COMMAND, DOWNLOADS_COMMAND

logger = logging.getLogger(__name__)

COMMANDS = frozenset({DOWNLOADS_COMMAND, DOWNLOAD_READ_COMMAND})
AGENT_BEHAVIOR_METHODS = {"Browser.setDownloadBehavior", "Page.setDownloadBehavior"}
MAX_READ_BYTES = 4 * 1024 * 1024
HASH_BLOCK_BYTES = 1024 * 1024


class DownloadState(str, Enum):
    """A download's state, as reported to the agent."""

    IN_PROGRESS = "in_progress"
    CHECKING = "checking"  # finished downloading; completion checks running
    COMPLETED = "completed"  # passed every check; the agent may read it
    BLOCKED = "blocked"
    CANCELED = "canceled"


class DownloadError(LocalCommandError):
    """A download command the proxy rejects. The message goes back to the agent."""


@dataclass(frozen=True)
class DownloadCheck:
    """The outcome of a download check: allowed, or the rule that blocked it."""

    allowed: bool
    rule: str | None = None


class DownloadPolicy(Protocol):
    """The checks a download goes through (the session's SessionDownloadPolicy, proxy/download_policy.py)."""

    async def download_begin(self, params: dict[str, Any], frame_id: str | None) -> tuple[DownloadCheck, str | None]:
        """Govern a download that began. Returns (the check, the page URL it began on)."""
        ...

    async def download_complete(self, params: dict[str, Any], begin_url: str | None) -> DownloadCheck:
        """Govern a finished download."""
        ...

    async def download_limit(self, reason: str, params: dict[str, Any]) -> None:
        """Record a system limit violation and end the session."""
        ...


Sender = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]]


@dataclass
class DownloadInfo:
    guid: str
    url: str
    name: str
    frame_id: str | None
    browser_context_id: str | None
    page_url: str | None = None
    state: DownloadState = DownloadState.IN_PROGRESS
    size: int | None = None
    sha256: str | None = None
    reason: str | None = None
    rule: str | None = None  # the rule that blocked it
    reserved_bytes: int = 0
    begun: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "name": self.name,
            "url": self.url,
            "state": self.state.value,
            "size": self.size,
            "sha256": self.sha256,
            "reason": self.reason,
            "rule": self.rule,
        }


class SessionDownloads:
    """Per-session download control: behavior, events, checks and retrieval."""

    def __init__(
        self,
        send: Sender,
        policy: DownloadPolicy,
        base_dir: Path | None,
        max_file_bytes: int,
        max_session_bytes: int,
    ) -> None:
        self._send = send
        self._policy = policy
        self.folder = SessionFolder("statelock-downloads-", base_dir)
        self.budget = ByteBudget("download", max_file_bytes, max_session_bytes)
        self._downloads: dict[str, DownloadInfo] = {}
        self._denied_contexts: set[str | None] = set()
        # browserContextId -> the task configuring it (None: the default context).
        self._contexts: dict[str | None, asyncio.Task[None]] = {}
        self._tasks = BackgroundTasks("downloads")

    @property
    def root(self) -> Path:
        return self.folder.path

    # Download behavior ------------------------------------------------------------------

    async def configure(self, browser_context_id: str | None = None) -> None:
        """Route a browser context's downloads to this session's folder (None: the default context)."""
        params: dict[str, Any] = {
            "behavior": "deny" if browser_context_id in self._denied_contexts else "allowAndName",
            "eventsEnabled": True,
        }
        if params["behavior"] != "deny":
            params["downloadPath"] = str(self.root)
        if browser_context_id is not None:
            params["browserContextId"] = browser_context_id
        await self._send("Browser.setDownloadBehavior", params)

    async def start(self) -> None:
        """Configure the default context. Its id (seen on the targets of a fresh browser)
        is not accepted by setDownloadBehavior, so it is mapped to the default (None)."""
        default = self._tasks.spawn(self.configure(None))
        self._contexts[None] = default
        await asyncio.shield(default)
        result = await self._send("Target.getTargets", None)
        for info in result.get("targetInfos", []):
            context_id = as_str(as_dict(info).get("browserContextId"))
            if context_id is not None:
                self._contexts.setdefault(context_id, default)

    def _canonical(self, browser_context_id: str | None) -> str | None:
        """None for the default context, whichever id it is known by."""
        default = self._contexts.get(None)
        if browser_context_id is not None and default is not None and self._contexts.get(browser_context_id) is default:
            return None
        return browser_context_id

    def context_ready(self, browser_context_id: str | None) -> Awaitable[None]:
        """Configure a context once. The agent's commands on its targets wait for this (fail closed)."""
        task = self._contexts.get(browser_context_id)
        if task is None:
            task = self._contexts[browser_context_id] = self._tasks.spawn(self.configure(browser_context_id))
        return asyncio.shield(task)

    async def apply_agent_request(self, method: str, params: dict[str, Any]) -> None:
        """The agent's own setDownloadBehavior: honour deny, otherwise keep Statelock's folder.

        Page.setDownloadBehavior names no context, so it applies to every context
        of the session (the stricter reading).
        """
        deny = params.get("behavior") == "deny"
        contexts = [as_str(params.get("browserContextId"))] if method == "Browser.setDownloadBehavior" else None
        targets = contexts if contexts is not None else [None, *self._contexts]
        for context_id in dict.fromkeys(self._canonical(context) for context in targets):
            if deny:
                self._denied_contexts.add(context_id)
            else:
                self._denied_contexts.discard(context_id)
            await self.configure(context_id)
        logger.info(
            "Agent %s behavior=%s applied by Statelock (its own download folder)", method, params.get("behavior")
        )

    # Browser events -----------------------------------------------------------------------

    def on_event(self, message: dict[str, Any]) -> None:
        """Statelock connection listener for Browser.download* events."""
        method = message.get("method")
        params = as_dict(message.get("params"))
        if method == "Browser.downloadWillBegin":
            self._tasks.spawn(self._begin(params))
        elif method == "Browser.downloadProgress":
            self._tasks.spawn(self._progress(params))

    def _params(self, info: DownloadInfo, phase: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "phase": phase,
            "guid": info.guid,
            "url": info.url,
            "suggested_filename": info.name,
            "download_count": len(self._downloads),
            "state": info.state.value,
        }
        for key in ("size", "sha256", "reason"):
            value = getattr(info, key)
            if value is not None:
                params[key] = value
        return params

    async def _begin(self, params: dict[str, Any]) -> None:
        guid = str(params.get("guid") or "")
        if not guid:
            return
        info = DownloadInfo(
            guid=guid,
            url=str(params.get("url") or ""),
            name=str(params.get("suggestedFilename") or guid),
            frame_id=as_str(params.get("frameId")),
            browser_context_id=as_str(params.get("browserContextId")),
        )
        self._downloads[guid] = info
        try:
            check, info.page_url = await self._policy.download_begin(self._params(info, "begin"), info.frame_id)
        except Exception:
            logger.exception("Download begin check failed for %s", info.name)
            check = DownloadCheck(allowed=False)
        if not check.allowed:
            await self._reject(info, "blocked by policy when the download began", check.rule)
        info.begun.set()

    async def _progress(self, params: dict[str, Any]) -> None:
        info = self._downloads.get(str(params.get("guid") or ""))
        if info is None:
            return
        state = params.get("state")
        received = params.get("receivedBytes")
        if state == "inProgress" and isinstance(received, (int, float)):
            await self._check_limits(info, int(received))
            return
        await info.begun.wait()
        if info.state == DownloadState.BLOCKED:
            self._delete(info)  # it may have finished before the cancel took effect
        elif state == "canceled" and info.state == DownloadState.IN_PROGRESS:
            info.state = DownloadState.CANCELED
            self._delete(info)
        elif state == "completed" and info.state == DownloadState.IN_PROGRESS:
            await self._complete(info)

    async def _limit(self, info: DownloadInfo, reason: str, phase: str) -> None:
        await self._reject(info, reason, SystemRule.DOWNLOAD_LIMIT.value)
        await self._policy.download_limit(reason, self._params(info, phase))

    async def _check_limits(self, info: DownloadInfo, received: int) -> None:
        if info.state != DownloadState.IN_PROGRESS:
            return
        problem = self.budget.violation(received)
        if problem is not None:
            await self._limit(info, f"{info.name}: {problem}", "progress")

    async def _complete(self, info: DownloadInfo) -> None:
        try:
            size, digest = await asyncio.to_thread(_hash_file, self.root / info.guid)
        except OSError as error:
            await self._reject(info, f"downloaded file is unreadable: {error}")
            return
        info.size, info.sha256 = size, digest
        problem = self.budget.violation(size)
        if problem is not None:
            await self._limit(info, f"{info.name}: {problem}", "complete")
            return
        # Reserve before any await, so concurrent completions cannot overrun the session budget.
        self.budget.reserve(size)
        info.reserved_bytes = size
        info.state = DownloadState.CHECKING
        try:
            check = await self._policy.download_complete(self._params(info, "complete"), info.page_url)
        except Exception:
            logger.exception("Download completion check failed for %s", info.name)
            check = DownloadCheck(allowed=False)
        if not check.allowed:
            await self._reject(info, "blocked by policy when the download completed", check.rule)
            return
        info.state = DownloadState.COMPLETED
        logger.info("Download %s completed (%d bytes, sha256=%s)", info.name, size, digest)

    async def _reject(self, info: DownloadInfo, reason: str, rule: str | None = None) -> None:
        if info.state == DownloadState.BLOCKED:
            return
        was_in_progress = info.state == DownloadState.IN_PROGRESS
        info.state = DownloadState.BLOCKED
        info.reason = reason
        info.rule = rule
        self.budget.release(info.reserved_bytes)
        info.reserved_bytes = 0
        if was_in_progress:
            params: dict[str, Any] = {"guid": info.guid}
            if info.browser_context_id is not None:
                params["browserContextId"] = info.browser_context_id
            try:
                await self._send("Browser.cancelDownload", params)
            except CdpError as error:  # already finished; the file is deleted below
                logger.debug("cancelDownload failed for %s: %s", info.guid, error)
        self._delete(info)
        logger.warning("Download %s blocked: %s", info.name, reason)

    def _delete(self, info: DownloadInfo) -> None:
        (self.root / info.guid).unlink(missing_ok=True)

    # Agent commands ---------------------------------------------------------------------

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one Statelock.download* command. Raises DownloadError.

        Runs on the event loop (the download table changes there); only file reads use a thread.
        """
        if method == DOWNLOADS_COMMAND:
            return {"downloads": [info.public() for info in self._downloads.values()]}
        if method == DOWNLOAD_READ_COMMAND:
            return await self.read(params.get("guid"), params.get("offset", 0), params.get("length", MAX_READ_BYTES))
        raise DownloadError(f"unknown Statelock command: {method}")

    async def read(self, guid: Any, offset: Any, length: Any) -> dict[str, Any]:
        info = self._downloads.get(guid) if isinstance(guid, str) else None
        if info is None:
            raise DownloadError(f"unknown download: {guid!r}")
        if info.state != DownloadState.COMPLETED:
            raise DownloadError(f"download {info.name} is not available (state={info.state.value})")
        if not isinstance(offset, int) or offset < 0 or not isinstance(length, int) or length <= 0:
            raise DownloadError("offset and length must be non-negative integers")
        try:
            data = await asyncio.to_thread(_read_chunk, self.root / info.guid, offset, min(length, MAX_READ_BYTES))
        except OSError as error:
            raise DownloadError(f"download {info.name} could not be read: {error}") from error
        return {"data": base64.b64encode(data).decode("ascii"), "eof": offset + len(data) >= (info.size or 0)}

    async def close(self) -> None:
        await self._tasks.close()
        self.folder.remove()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_BYTES), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _read_chunk(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)
