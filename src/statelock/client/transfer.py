# SPDX-License-Identifier: Apache-2.0
"""What the async (files.py, install.py) and sync (sync.py) clients share: the
Statelock upload, download and session protocol, and Playwright patching.

The protocol is written once, as generators of steps (``Command``, ``Sleep``,
``Write``) that receive each command's result. Each client only adds its
transport: a driver that performs the steps with ``await cdp.send(...)`` or
``cdp.send(...)`` and throws a failed command's error back into the steps.

Upload: ``Statelock.uploadFileBegin`` {name, mimeType} -> {uploadId}; one
``Statelock.uploadFileChunk`` {uploadId, data (base64)} per ``CHUNK_BYTES``;
``Statelock.uploadFileEnd`` {uploadId} -> {path, name, size, sha256, mimeType}. The
file input is found for the CDP session through a temporary marker attribute,
then filled with ``DOM.setFileInputFiles``.

Download: ``Statelock.downloads`` lists the session's downloads (guid, state, ...);
``Statelock.downloadRead`` {guid, offset, length} -> {data (base64), eof} streams a
completed one, checked against its SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import tempfile
import time
import uuid
from collections.abc import Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from statelock.wire import SESSION_COMMAND

# Per CDP message; stays well under uvicorn's 16 MiB WebSocket message limit.
CHUNK_BYTES = 4 * 1024 * 1024
UPLOAD_MARKER_ATTRIBUTE = "data-statelock-upload-target"
DOWNLOAD_POLL_INTERVAL = 0.1
DEFAULT_DOWNLOAD_TIMEOUT_MS = 30_000
# Chromium's answer to a method it does not have ("'Statelock.session' wasn't found").
UNKNOWN_COMMAND_TEXT = "wasn't found"

# A file to upload: a path, or a dict like Playwright's FilePayload {name, mimeType, buffer}.
UploadFile = str | Path | dict[str, Any]
FilePayload = tuple[str, str | None, bytes]
T = TypeVar("T")


class StatelockDownloadError(Exception):
    """A download could not be fetched (timeout, cancellation, integrity check)."""


class DownloadBlocked(Exception):
    """Statelock blocked the download; ``item`` is its entry from ``Statelock.downloads``."""

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item.get("reason") or f"Statelock blocked {item.get('name')}")
        self.item = item


def blocked_violation(blocked: DownloadBlocked, session_id: str | None) -> dict[str, Any]:
    """What a client knows about a blocked download; statelock_guard adds the recorded violation."""
    return {"rule": "download", "reason": str(blocked), "session_id": session_id}


# Steps --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """Send a CDP command; the step receives its result."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Sleep:
    seconds: float


@dataclass(frozen=True)
class Write:
    """Hand downloaded bytes to the caller's writer."""

    data: bytes


Step = Command | Sleep | Write
Steps = Generator[Step, Any, T]


def is_unknown_command(error: BaseException) -> bool:
    """A browser without Statelock answers its commands with this error."""
    return UNKNOWN_COMMAND_TEXT in str(error)


def session_steps() -> Steps[dict[str, Any] | None]:
    """{session_id, agent_id} from a Statelock browser, None from a plain one.
    Any other error (a closed page, a lost connection) says nothing and is raised."""
    try:
        return dict((yield Command(SESSION_COMMAND)))
    except Exception as error:
        if not is_unknown_command(error):
            raise
        return None


# Uploads ------------------------------------------------------------------------------------


def upload_items(files: UploadFile | Sequence[UploadFile]) -> list[UploadFile]:
    return [files] if isinstance(files, (str, Path, dict)) else list(files)


def file_payload(item: UploadFile) -> FilePayload:
    """(name, mime type, bytes) of one file to upload."""
    if isinstance(item, dict):
        buffer = item.get("buffer")
        if isinstance(buffer, (bytes, bytearray, memoryview)):
            data = bytes(buffer)
        else:
            data = str(buffer or "").encode("utf-8")
        return str(item["name"]), item.get("mimeType"), data
    path = Path(item)
    return path.name, mimetypes.guess_type(path.name)[0], path.read_bytes()


def file_payloads(files: UploadFile | Sequence[UploadFile]) -> list[FilePayload]:
    """Read the files to upload (blocking)."""
    return [file_payload(item) for item in upload_items(files)]


def upload_chunks(data: bytes) -> Iterator[str]:
    """The base64 chunks of ``data`` for ``Statelock.uploadFileChunk``."""
    for offset in range(0, len(data), CHUNK_BYTES):
        yield base64.b64encode(data[offset : offset + CHUNK_BYTES]).decode("ascii")


def upload_steps(payloads: Iterable[FilePayload]) -> Steps[list[dict[str, Any]]]:
    """Stream the files to Statelock. Returns what it stored (path, name, size, sha256, mimeType)."""
    uploaded = []
    for name, mime_type, data in payloads:
        begun = yield Command("Statelock.uploadFileBegin", {"name": name, "mimeType": mime_type})
        for chunk in upload_chunks(data):
            yield Command("Statelock.uploadFileChunk", {"uploadId": begun["uploadId"], "data": chunk})
        uploaded.append(dict((yield Command("Statelock.uploadFileEnd", {"uploadId": begun["uploadId"]}))))
    return uploaded


def new_marker() -> str:
    return str(uuid.uuid4())


SET_MARKER_SCRIPT = f"(el, marker) => el.setAttribute('{UPLOAD_MARKER_ATTRIBUTE}', marker)"
REMOVE_MARKER_SCRIPT = f"(el) => el.removeAttribute('{UPLOAD_MARKER_ATTRIBUTE}')"


def marker_selector(marker: str) -> str:
    return f'[{UPLOAD_MARKER_ATTRIBUTE}="{marker}"]'


def marked_node_id(query_result: dict[str, Any]) -> int:
    """The node id from ``DOM.querySelector`` for the marked file input."""
    node_id = query_result.get("nodeId")
    if not node_id:
        raise ValueError("set_input_files: the file input must be in the page's main frame")
    return int(node_id)


def file_paths(uploaded: Iterable[dict[str, Any]]) -> list[str]:
    """The proxy-side paths for ``DOM.setFileInputFiles``."""
    return [str(file["path"]) for file in uploaded]


def set_file_input_steps(marker: str, uploaded: list[dict[str, Any]]) -> Steps[None]:
    """Put the uploaded files into the file input carrying ``marker``."""
    document = yield Command("DOM.getDocument", {"depth": 0})
    found = yield Command(
        "DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": marker_selector(marker)}
    )
    yield Command("DOM.setFileInputFiles", {"nodeId": marked_node_id(found), "files": file_paths(uploaded)})


UPLOAD_OPTIONS = ("timeout", "no_wait_after", "strict")


def upload_timeout(options: dict[str, Any]) -> float | None:
    """Check Playwright's set_input_files / set_files options for a governed upload.

    ``timeout`` (ms) bounds the wait for the file input. ``no_wait_after`` has no
    effect in Playwright either. The input is always looked up strictly, so
    ``strict=False`` is refused rather than ignored.
    """
    unknown = sorted(set(options) - set(UPLOAD_OPTIONS))
    if unknown:
        raise TypeError(f"set_input_files: unexpected options {', '.join(unknown)}")
    if options.get("strict") is False:
        raise ValueError("set_input_files: strict=False is not supported through Statelock")
    timeout = options.get("timeout")
    return None if timeout is None else float(timeout)


# Downloads ----------------------------------------------------------------------------------


@dataclass
class DownloadInfo:
    """A completed download that passed Statelock's checks (it stays on the proxy until read)."""

    guid: str
    name: str
    url: str
    size: int
    sha256: str

    @property
    def suggested_filename(self) -> str:
        return self.name


def download_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The entries of a ``Statelock.downloads`` answer."""
    return [d for d in result.get("downloads", []) if isinstance(d, dict)]


def settle(item: dict[str, Any]) -> DownloadInfo | None:
    """A download entry's outcome: its info when completed, None while it runs.
    Raises DownloadBlocked or StatelockDownloadError (canceled)."""
    state = item.get("state")
    if state == "completed":
        return DownloadInfo(
            guid=str(item["guid"]),
            name=str(item.get("name")),
            url=str(item.get("url")),
            size=int(item.get("size") or 0),
            sha256=str(item.get("sha256")),
        )
    if state == "blocked":
        raise DownloadBlocked(item)
    if state == "canceled":
        raise StatelockDownloadError(f"download {item.get('name')} was canceled")
    return None


def first_new_download(entries: Iterable[dict[str, Any]], known: set[Any]) -> DownloadInfo | None:
    """The first download not in ``known`` that has finished (see settle)."""
    for item in entries:
        if item.get("guid") not in known:
            info = settle(item)
            if info is not None:
                return info
    return None


def known_downloads_steps() -> Steps[set[Any]]:
    """The guids Statelock already has: the download to wait for is the next one."""
    return {d.get("guid") for d in download_entries((yield Command("Statelock.downloads")))}


def wait_for_download_steps(known: set[Any], timeout: float) -> Steps[DownloadInfo]:
    """Poll until a download not in known completes. Raises DownloadBlocked if Statelock blocked it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = first_new_download(download_entries((yield Command("Statelock.downloads"))), known)
        if info is not None:
            return info
        yield Sleep(DOWNLOAD_POLL_INTERVAL)
    raise StatelockDownloadError(f"no download completed within {timeout} s")


def read_request(guid: str, offset: int) -> dict[str, Any]:
    return {"guid": guid, "offset": offset, "length": CHUNK_BYTES}


class DigestCheck:
    """SHA-256 over the chunks of a download, checked at the end."""

    def __init__(self, info: DownloadInfo) -> None:
        self.info = info
        self.offset = 0
        self._digest = hashlib.sha256()

    def chunk(self, result: dict[str, Any]) -> tuple[bytes, bool]:
        """Decode one ``Statelock.downloadRead`` answer: (bytes, whether it was the last)."""
        data = base64.b64decode(result["data"])
        self._digest.update(data)
        self.offset += len(data)
        return data, bool(result.get("eof")) or not data

    def verify(self) -> None:
        if self._digest.hexdigest() != self.info.sha256:
            raise StatelockDownloadError(f"{self.info.name}: content does not match the recorded SHA-256")


def read_steps(info: DownloadInfo) -> Steps[None]:
    """Stream a checked download in chunks (``Write``), verifying its SHA-256 at the end."""
    check = DigestCheck(info)
    while True:
        data, last = check.chunk((yield Command("Statelock.downloadRead", read_request(info.guid, check.offset))))
        yield Write(data)
        if last:
            break
    check.verify()


def download_timeout_seconds(timeout_ms: float | None) -> float:
    """Playwright's timeout (ms; None: the default; 0: none) in seconds."""
    timeout = DEFAULT_DOWNLOAD_TIMEOUT_MS if timeout_ms is None else timeout_ms
    return timeout / 1000 if timeout > 0 else float("inf")


def temporary_path(name: str) -> Path:
    """An empty temporary file for a download (blocking). The name comes from the
    page, so only its last part is used: it cannot leave the temporary folder."""
    handle, path = tempfile.mkstemp(prefix="statelock-", suffix=f"-{Path(name).name}")
    os.close(handle)
    return Path(path)


# Patching -----------------------------------------------------------------------------------


Patch = tuple[type, str, Any]


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
