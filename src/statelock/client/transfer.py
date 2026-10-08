# SPDX-License-Identifier: Apache-2.0
"""The Statelock upload, download and session protocol, shared by the async
(files.py) and sync (files_sync.py) clients.

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
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from statelock.client.violations import StatelockPolicyViolationError
from statelock.wire import (
    DOWNLOAD_READ_COMMAND,
    DOWNLOADS_COMMAND,
    SESSION_COMMAND,
    UPLOAD_BEGIN_COMMAND,
    UPLOAD_CHUNK_COMMAND,
    UPLOAD_END_COMMAND,
)

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
D = TypeVar("D")

# The upload's MIME type by file extension, the same table as the JS SDK's (js/src/mime.ts),
# so upload evidence does not depend on the SDK or on the machine's MIME database.
# Other extensions upload with no type.
MIME_TYPES = {
    ".7z": "application/x-7z-compressed",
    ".bmp": "image/bmp",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".eml": "message/rfc822",
    ".epub": "application/epub+zip",
    ".gif": "image/gif",
    ".gz": "application/gzip",
    ".heic": "image/heic",
    ".htm": "text/html",
    ".html": "text/html",
    ".ics": "text/calendar",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript",
    ".json": "application/json",
    ".md": "text/markdown",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".svg": "image/svg+xml",
    ".tar": "application/x-tar",
    ".tex": "application/x-tex",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webp": "image/webp",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
    ".zip": "application/zip",
}


class StatelockDownloadError(Exception):
    """A download could not be fetched (timeout, cancellation, integrity check)."""


class DownloadBlocked(Exception):
    """Statelock blocked the download; ``item`` is its entry from ``Statelock.downloads``."""

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item.get("reason") or f"Statelock blocked {item.get('name')}")
        self.item = item


def blocked_violation(blocked: DownloadBlocked, session_id: str | None) -> StatelockPolicyViolationError:
    """What a client knows about a blocked download: the rule Statelock reports in the
    download entry (None if it gives none) and the reason. The guards complete it with
    the recorded violation."""
    rule = blocked.item.get("rule")
    return StatelockPolicyViolationError(
        {"rule": rule if isinstance(rule, str) else None, "reason": str(blocked), "session_id": session_id}
    )


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


def buffer_bytes(buffer: Any) -> bytes:
    """A FilePayload's ``buffer``: bytes-like as is, text as UTF-8. Anything else (or no
    buffer) is refused rather than uploaded as its ``str()``, or as an empty file."""
    if isinstance(buffer, (bytes, bytearray, memoryview)):
        return bytes(buffer)
    if isinstance(buffer, str):
        return buffer.encode("utf-8")
    raise TypeError(f"set_input_files: buffer must be bytes or str, not {type(buffer).__name__}")


def mime_type(name: str) -> str | None:
    """The MIME type for a file name (MIME_TYPES), None for other extensions."""
    return MIME_TYPES.get(Path(name).suffix.lower())


def file_payload(item: UploadFile) -> FilePayload:
    """(name, mime type, bytes) of one file to upload."""
    if isinstance(item, dict):
        return str(item["name"]), item.get("mimeType"), buffer_bytes(item.get("buffer"))
    path = Path(item)
    return path.name, mime_type(path.name), path.read_bytes()


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
        begun = yield Command(UPLOAD_BEGIN_COMMAND, {"name": name, "mimeType": mime_type})
        for chunk in upload_chunks(data):
            yield Command(UPLOAD_CHUNK_COMMAND, {"uploadId": begun["uploadId"], "data": chunk})
        uploaded.append(dict((yield Command(UPLOAD_END_COMMAND, {"uploadId": begun["uploadId"]}))))
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
    return {d.get("guid") for d in download_entries((yield Command(DOWNLOADS_COMMAND)))}


def wait_for_download_steps(known: set[Any], timeout: float) -> Steps[DownloadInfo]:
    """Poll until a download not in known completes. Raises DownloadBlocked if Statelock blocked it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = first_new_download(download_entries((yield Command(DOWNLOADS_COMMAND))), known)
        if info is not None:
            return info
        yield Sleep(DOWNLOAD_POLL_INTERVAL)
    raise StatelockDownloadError(f"no download completed within {timeout} s")


def expect_download_steps(
    known: set[Any], timeout: float, accept: Callable[[DownloadInfo], D | None], session_id: str | None
) -> Steps[D]:
    """Wait for the first new download ``accept`` takes: it returns the download to hand
    over, or None to skip that one. ``timeout`` is in seconds for all of them together.
    A download Statelock blocked raises StatelockPolicyViolationError (blocked_violation)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            info = yield from wait_for_download_steps(known, max(0.0, deadline - time.monotonic()))
        except DownloadBlocked as blocked:
            raise blocked_violation(blocked, session_id) from None
        chosen = accept(info)
        if chosen is not None:
            return chosen
        known.add(info.guid)


class Waiter(Generic[D]):
    """What expect_download yields: the download, once the block has ended."""

    def __init__(self) -> None:
        self._value: D | None = None

    def resolve(self, value: D) -> None:
        """Hand over the download (expect_download does this after the block)."""
        self._value = value

    def result(self) -> D:
        if self._value is None:
            raise RuntimeError("the download is only available after the expect_download block has ended")
        return self._value


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
        data, last = check.chunk((yield Command(DOWNLOAD_READ_COMMAND, read_request(info.guid, check.offset))))
        yield Write(data)
        if last:
            break
    check.verify()


def download_timeout_seconds(timeout_ms: float | None, default_ms: float | None = None) -> float:
    """Playwright's timeout in seconds: ``timeout_ms``, else the page's default
    (``default_ms``, from set_default_timeout), else 30 s; 0 means no limit."""
    if timeout_ms is None:
        timeout_ms = DEFAULT_DOWNLOAD_TIMEOUT_MS if default_ms is None else default_ms
    return timeout_ms / 1000 if timeout_ms > 0 else float("inf")


def temporary_path(name: str) -> Path:
    """An empty temporary file for a download (blocking). The name comes from the
    page, so only its last part is used: it cannot leave the temporary folder."""
    handle, path = tempfile.mkstemp(prefix="statelock-", suffix=f"-{Path(name).name}")
    os.close(handle)
    return Path(path)
