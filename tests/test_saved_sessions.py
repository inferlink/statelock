"""Encrypted saved browser session storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from statelock.saved_sessions import (
    AesGcmCipher,
    SavedSessionError,
    SavedSessionKey,
    SavedSessionStore,
    _origin_of,
    check_key_location,
)
from statelock.services import saved_session_store
from statelock.settings import Settings

KEY = SavedSessionKey(tenant_id="acme", agent_id="finance_agent", name="bank-login")


def _store(tmp_path: Path) -> SavedSessionStore:
    return SavedSessionStore(tmp_path / "sessions", AesGcmCipher.from_key_file(tmp_path / "keys" / "saved.key"))


def test_payload_is_encrypted_with_aes_gcm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.save_payload(
        KEY,
        {
            "cookies": [{"name": "sessionid", "value": "super-secret-cookie", "domain": "example.test"}],
            "origins": [{"origin": "https://example.test", "localStorage": {"session_marker": "redacted-value"}}],
        },
    )
    raw = path.read_text(encoding="utf-8")
    assert "super-secret-cookie" not in raw
    assert "redacted-value" not in raw
    envelope = json.loads(raw)
    assert envelope["version"] == "statelock.saved_session.v2"
    assert envelope["cipher"] == "aes-256-gcm"

    payload = store.load_payload(KEY)
    assert payload is not None
    assert payload["cookies"][0]["value"] == "super-secret-cookie"
    assert payload["origins"][0]["localStorage"]["session_marker"] == "redacted-value"
    assert (tmp_path / "keys" / "saved.key").stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temporary file left


def test_tampering_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.save_payload(KEY, {"cookies": [], "origins": []})
    envelope = json.loads(path.read_text(encoding="utf-8"))
    ciphertext = envelope["ciphertext"]
    envelope["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SavedSessionError):
        store.load_payload(KEY)


def test_a_file_moved_to_another_agent_does_not_decrypt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.save_payload(KEY, {"cookies": [{"name": "sid", "value": "a"}], "origins": []})
    other = SavedSessionKey("acme", "other_agent", "bank-login")
    target = store.save_payload(other, {"cookies": [], "origins": []})
    target.write_bytes(source.read_bytes())
    with pytest.raises(SavedSessionError):
        store.load_payload(other)


def test_another_key_cannot_read(tmp_path: Path) -> None:
    _store(tmp_path).save_payload(KEY, {"cookies": [], "origins": []})
    other = SavedSessionStore(tmp_path / "sessions", AesGcmCipher.from_key_file(tmp_path / "other.key"))
    with pytest.raises(SavedSessionError):
        other.load_payload(KEY)


def test_list_and_delete_are_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_payload(SavedSessionKey("acme", "agent-a", "workday"), {"cookies": [], "origins": []})
    store.save_payload(SavedSessionKey("acme", "agent-b", "workday"), {"cookies": [], "origins": []})
    store.save_payload(SavedSessionKey("other", "agent-a", "erp"), {"cookies": [], "origins": []})
    assert store.names(tenant_id="acme", agent_id="agent-a") == ["workday"]
    assert store.delete(SavedSessionKey("acme", "agent-a", "workday"))
    assert store.names(tenant_id="acme", agent_id="agent-a") == []
    assert store.names(tenant_id="acme", agent_id="agent-b") == ["workday"]


def test_agents_whose_ids_differ_only_in_special_characters_do_not_share_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_payload(SavedSessionKey("acme", "a b", "login"), {"cookies": [], "origins": []})
    store.save_payload(SavedSessionKey("acme", "a_b", "login"), {"cookies": [{"name": "x"}], "origins": []})
    assert store.load_payload(SavedSessionKey("acme", "a b", "login")) is not None  # not overwritten
    assert store.names(tenant_id="acme", agent_id="a b") == ["login"]


def test_a_file_that_is_not_an_envelope_is_unreadable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.save_payload(KEY, {"cookies": [], "origins": []})
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SavedSessionError, match="unreadable"):
        store.load_payload(KEY)


def test_without_a_key_file_saved_sessions_are_off(tmp_path: Path) -> None:
    store = saved_session_store(Settings(artifact_dir=tmp_path))
    assert not store.enabled
    with pytest.raises(SavedSessionError, match="STATELOCK_SAVED_SESSIONS_KEY_FILE"):
        store.save_payload(KEY, {"cookies": [], "origins": []})


def test_the_key_file_must_be_outside_the_store(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        check_key_location(tmp_path / "sessions" / ".key", tmp_path / "sessions")
    with pytest.raises(ValueError, match="outside"):
        saved_session_store(Settings(artifact_dir=tmp_path, saved_sessions_key_file=tmp_path / "saved-sessions/k"))
    assert saved_session_store(Settings(artifact_dir=tmp_path, saved_sessions_key_file=tmp_path / "k")).enabled


def test_a_short_key_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "bad.key").write_bytes(b"short")
    with pytest.raises(ValueError, match="32 bytes"):
        AesGcmCipher.from_key_file(tmp_path / "bad.key")


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://user:pw@example.test/app", "https://example.test"),  # user info is not part of the origin
        ("https://example.test:443/app", "https://example.test"),  # default port dropped
        ("http://Example.TEST:8080/", "http://example.test:8080"),
        ("about:blank", None),
        ("chrome://settings", None),
        ("http://[::1", None),  # does not parse
    ],
)
def test_local_storage_origin_of_a_page(url: str, origin: str | None) -> None:
    assert _origin_of(url) == origin
