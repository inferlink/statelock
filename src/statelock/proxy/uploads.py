# SPDX-License-Identifier: Apache-2.0
"""Governed file uploads: files the agent sends to Statelock for DOM.setFileInputFiles.

Over a remote CDP connection the agent's files are not on the browser host.
The agent streams each file over its own CDP WebSocket with three Statelock
commands (answered by the proxy, never forwarded to Chromium):

- ``Statelock.uploadFileBegin`` {name, mimeType?} -> {uploadId}
- ``Statelock.uploadFileChunk`` {uploadId, data (base64)} -> {}
- ``Statelock.uploadFileEnd`` {uploadId} -> {path, name, size, sha256, mimeType}

The agent then sends ``DOM.setFileInputFiles`` with the returned paths. That
command is a governed action (state capture, pre-conditions, record), and it
may only name files uploaded in the same session, so an agent cannot attach
other files from the Statelock host. The client SDK wraps all of this in
``statelock.client.set_input_files``. Files are deleted when the session ends.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from statelock.proxy.files import ByteBudget, LocalCommandError, SessionFolder
from statelock.wire import UPLOAD_BEGIN_COMMAND, UPLOAD_CHUNK_COMMAND, UPLOAD_END_COMMAND

logger = logging.getLogger(__name__)

COMMANDS = frozenset({UPLOAD_BEGIN_COMMAND, UPLOAD_CHUNK_COMMAND, UPLOAD_END_COMMAND})
MAX_NAME_LENGTH = 255
MAX_PENDING_UPLOADS = 16


class UploadError(LocalCommandError):
    """An upload command the proxy rejects. The message goes back to the agent."""


@dataclass(frozen=True)
class UploadedFile:
    path: str
    name: str
    size: int
    sha256: str
    mime_type: str | None

    def as_dict(self) -> dict[str, Any]:
        """The file's evidence in the action record (snake_case, like the rest of the record)."""
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
        }

    def as_wire(self) -> dict[str, Any]:
        """The ``Statelock.uploadFileEnd`` answer (camelCase, like every CDP message)."""
        evidence = self.as_dict()
        return {**{k: v for k, v in evidence.items() if k != "mime_type"}, "mimeType": self.mime_type}


@dataclass
class _Pending:
    name: str
    mime_type: str | None
    path: Path
    size: int = 0
    digest: Any = field(default_factory=hashlib.sha256)


def safe_file_name(name: Any) -> str:
    """The file's base name as the site will see it. Rejects path tricks."""
    if not isinstance(name, str):
        raise UploadError("name must be a string")
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if base in {"", ".", ".."} or "\x00" in base or len(base) > MAX_NAME_LENGTH:
        raise UploadError(f"invalid file name: {name!r}")
    return base


class SessionUploads:
    """Per-session upload store, removed with the session."""

    def __init__(self, base_dir: Path | None, max_file_bytes: int, max_session_bytes: int) -> None:
        self.folder = SessionFolder("statelock-uploads-", base_dir)
        self.budget = ByteBudget("upload", max_file_bytes, max_session_bytes)
        self._pending: dict[str, _Pending] = {}
        self._files: dict[str, UploadedFile] = {}

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one Statelock.uploadFile* command. Raises UploadError."""
        if method == UPLOAD_BEGIN_COMMAND:
            return self.begin(params.get("name"), params.get("mimeType"))
        if method == UPLOAD_CHUNK_COMMAND:
            self.chunk(params.get("uploadId"), params.get("data"))
            return {}
        if method == UPLOAD_END_COMMAND:
            return self.end(params.get("uploadId")).as_wire()
        raise UploadError(f"unknown Statelock command: {method}")

    def begin(self, name: Any, mime_type: Any = None) -> dict[str, Any]:
        file_name = safe_file_name(name)
        if len(self._pending) >= MAX_PENDING_UPLOADS:
            raise UploadError(f"too many unfinished uploads (at most {MAX_PENDING_UPLOADS})")
        upload_id = str(uuid.uuid4())
        directory = self.folder.path / upload_id
        directory.mkdir()
        path = directory / file_name
        path.touch()
        self._pending[upload_id] = _Pending(
            name=file_name, mime_type=mime_type if isinstance(mime_type, str) else None, path=path
        )
        return {"uploadId": upload_id}

    def _pending_upload(self, upload_id: Any) -> _Pending:
        pending = self._pending.get(upload_id) if isinstance(upload_id, str) else None
        if pending is None:
            raise UploadError(f"unknown uploadId: {upload_id!r}")
        return pending

    def chunk(self, upload_id: Any, data: Any) -> None:
        pending = self._pending_upload(upload_id)
        if not isinstance(data, str):
            raise UploadError("data must be a base64 string")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise UploadError("data is not valid base64") from error
        problem = self.budget.violation(pending.size + len(raw), len(raw))
        if problem is not None:
            self._abort(upload_id, pending)
            raise UploadError(problem)
        self.budget.reserve(len(raw))
        with pending.path.open("ab") as handle:
            handle.write(raw)
        pending.size += len(raw)
        pending.digest.update(raw)

    def end(self, upload_id: Any) -> UploadedFile:
        pending = self._pending_upload(upload_id)
        del self._pending[upload_id]
        uploaded = UploadedFile(
            path=str(pending.path),
            name=pending.name,
            size=pending.size,
            sha256=pending.digest.hexdigest(),
            mime_type=pending.mime_type,
        )
        self._files[uploaded.path] = uploaded
        logger.info("Stored upload %s (%d bytes, sha256=%s)", uploaded.name, uploaded.size, uploaded.sha256)
        return uploaded

    def _abort(self, upload_id: str, pending: _Pending) -> None:
        self._pending.pop(upload_id, None)
        self.budget.release(pending.size)
        shutil.rmtree(pending.path.parent, ignore_errors=True)

    def resolve(self, paths: Any) -> list[UploadedFile]:
        """The uploaded files named by a DOM.setFileInputFiles command. Raises UploadError."""
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise UploadError("files must be a list of paths")
        resolved = []
        for path in paths:
            uploaded = self._files.get(path)
            if uploaded is None:
                raise UploadError(
                    f"{path} was not uploaded in this Statelock session. Upload files with "
                    "statelock.client.set_input_files (or the Statelock.uploadFile* commands) first."
                )
            resolved.append(uploaded)
        return resolved

    def close(self) -> None:
        self.folder.remove()
        self._pending.clear()
        self._files.clear()
