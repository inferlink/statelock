# SPDX-License-Identifier: Apache-2.0
"""Encrypted saved browser sessions.

A saved session lets an agent reuse a site login without ever holding the
cookies: Statelock restores the saved cookies and localStorage into the
session's browser before the agent connects, and saves them again when a
session that asked for it ends cleanly.

Files are encrypted with AES-256-GCM (the ``cryptography`` package). The
tenant, agent and session name are bound to each file as associated data, so a
file copied to another agent's name does not decrypt. The key comes from
``STATELOCK_SAVED_SESSIONS_KEY_FILE``, which must live outside the saved-session
directory; without it, saved sessions are off. The Enclave replaces the cipher
(for example with a KMS-wrapped data key) through ``SavedSessionCipher``.

Restored: cookies (``Storage.setCookies``) and each saved origin's localStorage.
Not restored: sessionStorage (per tab, like a browser restart), IndexedDB, and
storage of cross-origin iframes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from statelock.core.jsonutil import as_dict
from statelock.fileio import write_atomic
from statelock.proxy.connection import CdpConnection, CdpError
from statelock.proxy.pages import PageSessions

logger = logging.getLogger(__name__)

SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
FILE_VERSION = "statelock.saved_session.v2"
FILE_SUFFIX = ".slsession"
KEY_BYTES = 32
NONCE_BYTES = 12
# Seconds to open a blank page at an origin while restoring its localStorage.
ORIGIN_RESTORE_TIMEOUT = 5.0
EMPTY_PAGE = base64.b64encode(b"<!doctype html><title></title>").decode("ascii")
NOT_CONFIGURED = "saved sessions are off: set STATELOCK_SAVED_SESSIONS_KEY_FILE"


class SavedSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SavedSessionKey:
    tenant_id: str | None
    agent_id: str
    name: str

    def validate(self) -> None:
        if not SESSION_NAME.match(self.name):
            raise SavedSessionError("saved session names must be 1-80 letters, digits, '.', '_' or '-'")

    def associated_data(self) -> bytes:
        """Bound to the ciphertext: the file only decrypts under this tenant, agent and name."""
        return json.dumps([FILE_VERSION, self.tenant_id, self.agent_id, self.name]).encode("utf-8")


class SavedSessionCipher(Protocol):
    """Encrypts saved-session payloads. ``name`` is stored in each file."""

    name: str

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> dict[str, str]: ...

    def decrypt(self, envelope: dict[str, Any], associated_data: bytes) -> bytes: ...


class AesGcmCipher:
    """AES-256-GCM with a local 32-byte key."""

    name = "aes-256-gcm"

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(f"the saved-session key must be {KEY_BYTES} bytes")
        self._aead = AESGCM(key)

    @classmethod
    def from_key_file(cls, path: Path) -> AesGcmCipher:
        """Read the key, or create it (mode 0600) when the file does not exist yet."""
        if path.exists():
            key = path.read_bytes()
            if len(key) != KEY_BYTES:
                raise ValueError(f"saved-session key file must contain {KEY_BYTES} bytes: {path}")
            return cls(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        key = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        logger.warning("Created saved-session key %s. Back it up: without it saved sessions cannot be read.", path)
        return cls(key)

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> dict[str, str]:
        nonce = os.urandom(NONCE_BYTES)
        return {"nonce": _b64(nonce), "ciphertext": _b64(self._aead.encrypt(nonce, plaintext, associated_data))}

    def decrypt(self, envelope: dict[str, Any], associated_data: bytes) -> bytes:
        try:
            return self._aead.decrypt(
                _unb64(str(envelope["nonce"])), _unb64(str(envelope["ciphertext"])), associated_data
            )
        except InvalidTag as error:
            raise ValueError("saved-session authentication failed") from error


def check_key_location(key_file: Path, root_dir: Path) -> None:
    """The key must not sit inside the directory it protects."""
    key_path, root = key_file.resolve(), root_dir.resolve()
    if key_path == root or root in key_path.parents:
        raise ValueError(
            f"STATELOCK_SAVED_SESSIONS_KEY_FILE ({key_file}) must be outside the saved-session directory ({root_dir})"
        )


class SavedSessionStore:
    def __init__(self, root_dir: str | Path, cipher: SavedSessionCipher | None = None) -> None:
        self.root_dir = Path(root_dir)
        self.cipher = cipher

    @property
    def enabled(self) -> bool:
        return self.cipher is not None

    def names(self, *, tenant_id: str | None, agent_id: str) -> list[str]:
        directory = self._agent_dir(tenant_id, agent_id)
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob(f"*{FILE_SUFFIX}") if path.is_file())

    def delete(self, key: SavedSessionKey) -> bool:
        key.validate()
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            return False
        return True

    def load_payload(self, key: SavedSessionKey) -> dict[str, Any] | None:
        key.validate()
        cipher = self._cipher()
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            envelope = _json_object(path.read_text(encoding="utf-8"))
            supported = envelope.get("version") == FILE_VERSION and envelope.get("cipher") == cipher.name
            payload = json.loads(cipher.decrypt(envelope, key.associated_data())) if supported else None
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            raise SavedSessionError(f"saved session is unreadable: {key.name}") from error
        if not supported:
            raise SavedSessionError(f"saved session has an unsupported version or cipher: {key.name}")
        return payload if isinstance(payload, dict) else None

    def save_payload(self, key: SavedSessionKey, payload: dict[str, Any]) -> Path:
        key.validate()
        cipher = self._cipher()
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"saved_at": datetime.now(timezone.utc).isoformat(), **payload}
        plaintext = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {"version": FILE_VERSION, "cipher": cipher.name, **cipher.encrypt(plaintext, key.associated_data())}
        # A crash or a concurrent save never leaves a partial file; only the owner can read it.
        write_atomic(path, json.dumps(envelope, sort_keys=True).encode("utf-8"), mode=0o600)
        return path

    async def restore(self, connection: CdpConnection, key: SavedSessionKey) -> bool:
        """Put the saved cookies and localStorage into the browser. False if there is none yet."""
        payload = await asyncio.to_thread(self.load_payload, key)  # a cipher may call a key service
        if payload is None:
            logger.info("No saved browser session %s for agent_id=%s yet", key.name, key.agent_id)
            return False
        cookies = [cookie for cookie in payload.get("cookies") or [] if isinstance(cookie, dict)]
        if cookies:
            await connection.send("Storage.setCookies", {"cookies": cookies})
        for item in payload.get("origins") or []:
            origin, storage = _origin_entry(item)
            if origin and storage:
                await _write_local_storage(connection, origin, storage)
        logger.info("Restored saved browser session %s for agent_id=%s", key.name, key.agent_id)
        return True

    async def save_from_browser(self, connection: CdpConnection, pages: PageSessions, key: SavedSessionKey) -> Path:
        cookies = as_dict(await connection.send("Storage.getCookies")).get("cookies") or []
        origins = await _read_local_storage(connection, pages)
        state = {"cookies": [c for c in cookies if isinstance(c, dict)], "origins": origins}
        path = await asyncio.to_thread(self.save_payload, key, state)
        logger.info("Saved browser session %s for agent_id=%s", key.name, key.agent_id)
        return path

    def _cipher(self) -> SavedSessionCipher:
        if self.cipher is None:
            raise SavedSessionError(NOT_CONFIGURED)
        return self.cipher

    def _path(self, key: SavedSessionKey) -> Path:
        return self._agent_dir(key.tenant_id, key.agent_id) / f"{key.name}{FILE_SUFFIX}"

    def _agent_dir(self, tenant_id: str | None, agent_id: str) -> Path:
        return self.root_dir / _slug(tenant_id or "default") / _slug(agent_id)


def _origin_entry(item: Any) -> tuple[str | None, dict[str, str]]:
    if not isinstance(item, dict) or not isinstance(item.get("origin"), str):
        return None, {}
    return item["origin"], _string_map(item.get("localStorage"))


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


async def _read_local_storage(connection: CdpConnection, pages: PageSessions) -> list[dict[str, Any]]:
    """localStorage of each open page's origin, read over CDP (no script runs in the page)."""
    origins: dict[str, dict[str, str]] = {}
    for target in await pages.list_targets():
        origin = _origin_of(str(target.get("url") or ""))
        if target.get("type") != "page" or not target.get("targetId") or origin is None or origin in origins:
            continue
        try:
            session_id = await pages.session_for(str(target["targetId"]))
            result = await connection.send(
                "DOMStorage.getDOMStorageItems",
                {"storageId": {"securityOrigin": origin, "isLocalStorage": True}},
                session_id=session_id,
            )
        except CdpError as error:
            logger.warning("Could not read localStorage of %s: %s", origin, error)
            continue
        entries = result.get("entries") or []
        origins[origin] = {str(e[0]): str(e[1]) for e in entries if isinstance(e, list) and len(e) == 2}
    return [{"origin": origin, "localStorage": items} for origin, items in origins.items() if items]


async def _write_local_storage(connection: CdpConnection, origin: str, items: dict[str, str]) -> None:
    """Open a hidden page at the origin, answered locally (nothing reaches the site), and set the items.

    Chromium only writes an origin's localStorage from a frame of that origin.
    """
    created = await connection.send("Target.createTarget", {"url": "about:blank", "background": True})
    target_id = str(created["targetId"])
    try:
        session_id = str(
            (await connection.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        )
        remove = _fulfill_locally(connection, session_id)
        try:
            await connection.send(
                "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, session_id=session_id
            )
            await connection.send(
                "Page.navigate", {"url": origin + "/"}, session_id=session_id, timeout=ORIGIN_RESTORE_TIMEOUT
            )
            for name, value in items.items():
                await connection.send(
                    "DOMStorage.setDOMStorageItem",
                    {"storageId": {"securityOrigin": origin, "isLocalStorage": True}, "key": name, "value": value},
                    session_id=session_id,
                )
        finally:
            remove()
    finally:
        with contextlib.suppress(CdpError):
            await connection.send("Target.closeTarget", {"targetId": target_id})


def _fulfill_locally(connection: CdpConnection, session_id: str) -> Callable[[], None]:
    """Answer every request of the restore page with an empty document. Returns the remover."""
    tasks: set[asyncio.Task[Any]] = set()

    def on_event(message: dict[str, Any]) -> None:
        if message.get("method") != "Fetch.requestPaused" or message.get("sessionId") != session_id:
            return
        request_id = as_dict(message.get("params")).get("requestId")
        task = asyncio.ensure_future(
            connection.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": 200,
                    "body": EMPTY_PAGE,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                },
                session_id=session_id,
            )
        )
        tasks.add(task)
        task.add_done_callback(_forget(tasks))

    connection.add_listener(on_event)
    return lambda: connection.remove_listener(on_event)


def _forget(tasks: set[asyncio.Task[Any]]) -> Callable[[asyncio.Task[Any]], None]:
    def done(task: asyncio.Task[Any]) -> None:
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.debug("Fetch.fulfillRequest failed during restore: %s", task.exception())

    return done


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("not a JSON object")
    return value


def _slug(value: str) -> str:
    """A file-name-safe form of an id. When characters had to change, a short hash of the
    original keeps two ids apart ("a b" and "a_b" would otherwise share a folder)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-") or "default"
    if slug != value:
        slug = f"{slug}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:10]}"
    return slug


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
