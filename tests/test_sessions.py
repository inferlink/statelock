"""Session URLs: issuing, discovery, single use, expiry, log redaction."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from statelock.app import create_app
from statelock.auth import Identity, hash_key
from statelock.client import sessions as client_sessions
from statelock.saved_sessions import SavedSessionKey
from statelock.sessions import install_log_redaction, redact_tokens
from statelock.settings import MAX_SESSION_URL_TTL, Settings
from statelock.tokens import MAX_PENDING_PER_AGENT, SessionTokens, TooManyPendingSessions

AGENT = "finance_reconciliation_agent"  # registered in the policy_file fixture


def _client(policy_file: Path, tmp_path: Path, **overrides: object) -> TestClient:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {"agent_id": AGENT, "tenant": "acme", "key_sha256": hash_key("slk_agent")},
                    {"agent_id": "other_agent", "tenant": "acme", "key_sha256": hash_key("slk_other")},
                ],
                "reviewers": [{"reviewer_id": "alice", "key_sha256": hash_key("slk_alice")}],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        policy_file=policy_file,
        artifact_dir=tmp_path / "artifacts",
        plugins="none",
        auth_keys_file=keys,
        saved_sessions_key_file=tmp_path / "saved-sessions.key",
    )
    return TestClient(create_app(settings.model_copy(update=overrides)))


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_session_url_is_issued_for_the_keys_agent(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path)
    response = client.post("/sessions", json={}, headers=_bearer("slk_agent"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == AGENT
    assert body["cdp_url"].startswith("http://testserver/sessions/slt_")
    assert body["ws_url"] == body["cdp_url"].replace("http://", "ws://") + "/devtools"

    token_path = body["cdp_url"].removeprefix("http://testserver")
    version = client.get(token_path + "/json/version")
    assert version.status_code == 200
    assert version.json()["webSocketDebuggerUrl"] == body["ws_url"]
    assert client.get(token_path + "/json/version/").status_code == 200  # Playwright adds a slash


def test_session_url_can_request_saved_session(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path)
    response = client.post(
        "/sessions",
        json={"saved_session_name": "bank-login", "save_session": True},
        headers=_bearer("slk_agent"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved_session_name"] == "bank-login"
    assert body["save_session"] is True


def test_saved_sessions_need_a_key_file(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path, saved_sessions_key_file=None)
    response = client.post("/sessions", json={"saved_session_name": "bank-login"}, headers=_bearer("slk_agent"))
    assert response.status_code == 422
    assert "STATELOCK_SAVED_SESSIONS_KEY_FILE" in response.text


def test_client_requests_saved_sessions(policy_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """create_session_url, list_saved_sessions and delete_saved_session against the app."""
    client = _client(policy_file, tmp_path)
    calls: list[tuple[str, str, object]] = []

    def call(method: str, path: str, _server: object, _key: object, body: object = None) -> dict[str, object]:
        calls.append((method, path, body))
        response = client.request(method, path, json=body, headers=_bearer("slk_agent"))
        assert response.status_code == 200, response.text
        return dict(response.json())

    monkeypatch.setattr(client_sessions, "request_json", call)
    url = client_sessions.create_session_url("http://x", saved_session="bank-login", save_session=True)
    assert url.saved_session == "bank-login"
    assert calls[0][2] == {"saved_session_name": "bank-login", "save_session": True}
    with pytest.raises(client_sessions.StatelockClientError):
        client_sessions.create_session_url("http://x", save_session=True)

    client.app.state.services.saved_sessions.save_payload(SavedSessionKey("acme", AGENT, "bank-login"), {})
    assert client_sessions.list_saved_sessions("http://x") == ["bank-login"]
    assert client_sessions.delete_saved_session("bank-login", "http://x") is True
    assert client_sessions.delete_saved_session("bank-login", "http://x") is False


def test_save_session_needs_a_name(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path)
    response = client.post("/sessions", json={"save_session": True}, headers=_bearer("slk_agent"))
    assert response.status_code == 422


def test_saved_sessions_are_listed_and_deleted_for_the_keys_agent(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path)
    services = client.app.state.services
    services.saved_sessions.save_payload(
        key=SavedSessionKey("acme", AGENT, "bank-login"),
        payload={"cookies": [], "origins": []},
    )

    listed = client.get("/saved-sessions", headers=_bearer("slk_agent"))
    assert listed.status_code == 200, listed.text
    assert listed.json()["saved_sessions"] == ["bank-login"]

    deleted = client.delete("/saved-sessions/bank-login", headers=_bearer("slk_agent"))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert client.get("/saved-sessions", headers=_bearer("slk_agent")).json()["saved_sessions"] == []


def test_session_url_needs_an_agent_key(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path)
    assert client.post("/sessions", json={}).status_code == 401
    assert client.post("/sessions", json={}, headers=_bearer("slk_alice")).status_code == 401  # a reviewer key
    assert client.post("/sessions", json={"agent_id": AGENT}, headers=_bearer("slk_other")).status_code == 403
    # A key for an agent without a policy: refused.
    assert client.post("/sessions", json={}, headers=_bearer("slk_other")).status_code == 403
    assert client.post("/sessions", json={"ttl_seconds": 99999}, headers=_bearer("slk_agent")).status_code == 422
    assert client.get("/sessions/slt_unknown/json/version").status_code == 404


def test_session_url_without_authentication_names_the_agent(policy_file: Path, tmp_path: Path) -> None:
    client = _client(policy_file, tmp_path, auth_mode="none")
    assert client.post("/sessions", json={}).status_code == 422
    assert client.post("/sessions", json={"agent_id": AGENT}).json()["agent_id"] == AGENT


def test_tokens_are_single_use_and_expire() -> None:
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    tokens = SessionTokens(clock=lambda: now[0])
    identity = Identity(agent_id="a", tenant="t", authenticated=True)
    token, pending = tokens.issue(identity, ttl_seconds=60)
    assert tokens.peek(token) == pending
    assert tokens.consume(token) == pending
    assert tokens.consume(token) is None  # used up
    short, _ = tokens.issue(identity, ttl_seconds=10)
    now[0] += timedelta(seconds=9)
    assert tokens.peek(short) is not None
    now[0] += timedelta(seconds=1)
    assert tokens.peek(short) is None


def test_tokens_are_redacted_from_logs() -> None:
    assert redact_tokens("GET /sessions/slt_abc-DEF_123/json/version") == "GET /sessions/slt_[redacted]/json/version"
    install_log_redaction()
    install_log_redaction()  # once is enough; a second call does not wrap again
    # Records of any logger, children of statelock included (a logger filter would miss those).
    for name in ("uvicorn.access", "statelock.proxy.bridge", "other.library"):
        record = logging.getLogger(name).makeRecord(
            name, logging.INFO, "", 0, '%s "%s"', ("1.2.3.4", "/sessions/slt_x1"), None
        )
        assert record.getMessage() == '1.2.3.4 "/sessions/slt_[redacted]"'


def test_an_agent_holds_a_bounded_number_of_unused_urls() -> None:
    tokens = SessionTokens()
    identity = Identity(agent_id="a", tenant="t", authenticated=True)
    for _ in range(MAX_PENDING_PER_AGENT):
        tokens.issue(identity, ttl_seconds=60)
    with pytest.raises(TooManyPendingSessions):
        tokens.issue(identity, ttl_seconds=60)
    tokens.issue(Identity(agent_id="b", tenant="t", authenticated=True), ttl_seconds=60)  # others are not affected


def test_server_default_session_url_ttl_is_capped_like_requests() -> None:
    assert Settings(session_url_ttl=MAX_SESSION_URL_TTL).session_url_ttl == MAX_SESSION_URL_TTL
    with pytest.raises(ValidationError, match="session_url_ttl"):
        Settings(session_url_ttl=MAX_SESSION_URL_TTL + 1)
