# SPDX-License-Identifier: Apache-2.0
"""Shared pieces of governed uploads and downloads: session folders, byte budgets, errors."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


class LocalCommandError(Exception):
    """A Statelock.* command the proxy rejects. The message goes back to the agent."""


class SessionFolder:
    """A per-session temporary folder, created on first use and removed with the session."""

    def __init__(self, prefix: str, base_dir: Path | None) -> None:
        self._prefix = prefix
        self._base_dir = base_dir
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            if self._base_dir is not None:
                self._base_dir.mkdir(parents=True, exist_ok=True)
            self._path = Path(tempfile.mkdtemp(prefix=self._prefix, dir=self._base_dir)).resolve()
        return self._path

    @property
    def exists(self) -> bool:
        return self._path is not None and self._path.exists()

    def remove(self) -> None:
        if self._path is not None:
            shutil.rmtree(self._path, ignore_errors=True)
            self._path = None


class ByteBudget:
    """Per-file and per-session byte limits. Bytes are reserved before they are used."""

    def __init__(self, kind: str, max_file_bytes: int, max_session_bytes: int) -> None:
        self.kind = kind
        self.max_file_bytes = max_file_bytes
        self.max_session_bytes = max_session_bytes
        self.used = 0

    def violation(self, file_bytes: int, extra_session_bytes: int | None = None) -> str | None:
        """Why a file of this size does not fit, or None. extra defaults to the file size."""
        extra = file_bytes if extra_session_bytes is None else extra_session_bytes
        if file_bytes > self.max_file_bytes:
            return f"file exceeds the {self.kind} limit of {self.max_file_bytes} bytes"
        if self.used + extra > self.max_session_bytes:
            return f"session exceeds the {self.kind} limit of {self.max_session_bytes} bytes"
        return None

    def reserve(self, amount: int) -> None:
        self.used += amount

    def release(self, amount: int) -> None:
        self.used = max(0, self.used - amount)
