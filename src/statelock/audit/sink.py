# SPDX-License-Identifier: Apache-2.0
"""Artifact sink interface and the local JSON sink."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from statelock.audit.records import ActionRecord
from statelock.fileio import write_atomic

logger = logging.getLogger(__name__)

NETWORK_LOG_FILENAME = "network.jsonl"
CONTEXT_FILENAME = "context.json"
_ACTION_DIR = re.compile(r"^action-(\d+)$")


@runtime_checkable
class ArtifactSink(Protocol):
    """Where governed-action evidence is stored.

    Records arrive in sequence order per session. Implementations: LocalJsonSink
    here. Plugins can wrap or replace the sink (for example, with tamper-evident
    or remote storage) through ``ctx.services.sink``.
    """

    def session_exists(self, session_id: str) -> bool: ...

    async def write_action(self, record: ActionRecord) -> str:
        """Store one action record. Returns a location string for logs."""
        ...

    async def read_action(self, session_id: str, sequence: int) -> dict[str, Any] | None: ...

    async def read_screenshot(self, session_id: str, sequence: int, filename: str) -> bytes | None: ...

    async def list_actions(self, session_id: str) -> list[int]: ...

    async def append_network_record(self, session_id: str, record: dict[str, Any]) -> None: ...

    async def read_network_records(self, session_id: str) -> list[dict[str, Any] | None]:
        """All network log lines of a session in write order. ``None`` marks an unreadable line."""
        ...


class LocalJsonSink:
    """Development sink: artifacts/sessions/<session_id>/action-XXXX/{context.json,*.jpg}."""

    def __init__(self, root_dir: str | Path, *, write_latest: bool = False) -> None:
        self.root_dir = Path(root_dir)
        self.write_latest = write_latest

    def _session_dir(self, session_id: str) -> Path:
        return self.root_dir / "sessions" / session_id

    def _action_dir(self, session_id: str, sequence: int) -> Path:
        return self._session_dir(session_id) / f"action-{sequence:04d}"

    def session_exists(self, session_id: str) -> bool:
        return self._session_dir(session_id).exists()

    async def write_action(self, record: ActionRecord) -> str:
        return await asyncio.to_thread(self._write_action_sync, record)

    def _write_action_sync(self, record: ActionRecord) -> str:
        action_dir = self._action_dir(record.session_id, record.sequence)
        action_dir.mkdir(parents=True, exist_ok=True)
        for filename, data in record.screenshots.items():
            write_atomic(action_dir / filename, data)
        # context.json last: a record is complete once it exists.
        write_atomic(action_dir / CONTEXT_FILENAME, json.dumps(record.payload, indent=2, sort_keys=True).encode())
        if self.write_latest:
            self._write_latest(record, action_dir)
        return str(action_dir)

    def _write_latest(self, record: ActionRecord, action_dir: Path) -> None:
        latest = {
            "session_id": record.session_id,
            "sequence": record.sequence,
            "action_dir": str(action_dir.relative_to(self.root_dir)),
            "screenshots": sorted(record.screenshots),
            "policy_violation": record.payload.get("policy_violation"),
            "post_policy_violation": record.payload.get("post_policy_violation"),
        }
        write_atomic(self.root_dir / "latest.json", json.dumps(latest, indent=2, sort_keys=True).encode())

    async def read_action(self, session_id: str, sequence: int) -> dict[str, Any] | None:
        path = self._action_dir(session_id, sequence) / CONTEXT_FILENAME
        return await asyncio.to_thread(_read_json, path)

    async def read_screenshot(self, session_id: str, sequence: int, filename: str) -> bytes | None:
        if "/" in filename or "\\" in filename:
            return None
        path = self._action_dir(session_id, sequence) / filename
        return await asyncio.to_thread(_read_bytes, path)

    async def list_actions(self, session_id: str) -> list[int]:
        session_dir = self._session_dir(session_id)

        def scan() -> list[int]:
            if not session_dir.is_dir():
                return []
            sequences = []
            for path in session_dir.iterdir():
                match = _ACTION_DIR.match(path.name)
                if match and path.is_dir():
                    sequences.append(int(match.group(1)))
            return sorted(sequences)

        return await asyncio.to_thread(scan)

    async def append_network_record(self, session_id: str, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._append_network_sync, session_id, record)

    def _append_network_sync(self, session_id: str, record: dict[str, Any]) -> None:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        line = {"recorded_at": datetime.now(timezone.utc).isoformat(), **record}
        with (session_dir / NETWORK_LOG_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")

    async def read_network_records(self, session_id: str) -> list[dict[str, Any] | None]:
        path = self._session_dir(session_id) / NETWORK_LOG_FILENAME
        return await asyncio.to_thread(_read_jsonl, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any] | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any] | None] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            records.append(None)
            continue
        records.append(value if isinstance(value, dict) else None)
    return records


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None
