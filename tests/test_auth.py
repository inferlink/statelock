from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from statelock.__main__ import main
from statelock.app import create_app
from statelock.auth import Authenticator, AuthError, KeysFile, bearer_token, generate_key, hash_key, load_keys_file
from statelock.settings import Settings

AGENT = "finance_reconciliation_agent"
KEY = "slk_test_key_for_finance"
OTHER_KEY = "slk_test_key_for_other"


def _keys(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "keys.yaml"
    default = [
        {"agent_id": AGENT, "tenant": "acme", "key_sha256": hash_key(KEY)},
        {"agent_id": "other_agent", "tenant": "globex", "key_sha256": hash_key(OTHER_KEY)},
    ]
    path.write_text(yaml.safe_dump({"agents": list(entries) or default}), encoding="utf-8")
    return path


def _auth(tmp_path: Path, *entries: dict) -> Authenticator:
    return Authenticator("api_key", load_keys_file(_keys(tmp_path, *entries)))


def test_generated_keys_are_random_and_prefixed() -> None:
    first, second = generate_key(), generate_key()
    assert first != second
    assert first.startswith("slk_")
    assert len(first) > 40


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer  abc ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer ") is None
    assert bearer_token(None) is None


def test_authenticate_resolves_agent_and_tenant(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    identity = auth.authenticate(None, f"Bearer {KEY}")
    assert (identity.agent_id, identity.tenant, identity.authenticated) == (AGENT, "acme", True)
    assert auth.authenticate(AGENT, f"Bearer {KEY}").agent_id == AGENT


@pytest.mark.parametrize(
    ("claimed", "header", "message"),
    [
        (AGENT, None, "missing"),
        (AGENT, "Bearer slk_wrong", "unknown key"),
        (AGENT, f"Bearer {OTHER_KEY}", "belongs to other_agent"),
        (None, f"Basic {KEY}", "missing"),
    ],
)
def test_authenticate_rejects(tmp_path: Path, claimed: str | None, header: str | None, message: str) -> None:
    with pytest.raises(AuthError, match=message):
        _auth(tmp_path).authenticate(claimed, header)


def test_disabled_and_expired_keys(tmp_path: Path) -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    auth = _auth(
        tmp_path,
        {"agent_id": "a", "key_sha256": hash_key("k1"), "disabled": True},
        {"agent_id": "a", "key_sha256": hash_key("k2"), "expires": past},
        {"agent_id": "a", "key_sha256": hash_key("k3"), "expires": future},
    )
    with pytest.raises(AuthError, match="disabled"):
        auth.authenticate(None, "Bearer k1")
    with pytest.raises(AuthError, match="expired"):
        auth.authenticate(None, "Bearer k2")
    assert auth.authenticate(None, "Bearer k3").tenant == "default"


def test_keys_file_validation(tmp_path: Path) -> None:
    for bad in (
        {"agents": [{"agent_id": "a", "key_sha256": "abc"}]},
        {"agents": [{"agent_id": "a", "key_sha256": hash_key("k"), "key": "k"}]},
        {"agents": [{"agent_id": "a", "key_sha256": hash_key("k")}, {"agent_id": "b", "key_sha256": hash_key("k")}]},
        {
            "agents": [
                {"agent_id": "a", "tenant": "x", "key_sha256": hash_key("k1")},
                {"agent_id": "a", "tenant": "y", "key_sha256": hash_key("k2")},
            ]
        },
    ):
        with pytest.raises(ValidationError):
            KeysFile.model_validate(bad)
    with pytest.raises(FileNotFoundError):
        load_keys_file(tmp_path / "missing.yaml")
    with pytest.raises(FileNotFoundError):
        load_keys_file(tmp_path)


def test_api_key_mode_requires_keys_file(policy_file: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="STATELOCK_AUTH_KEYS_FILE"):
        create_app(Settings(policy_file=policy_file, artifact_dir=tmp_path / "a", plugins="none"))


def test_mode_none_trusts_the_claimed_agent() -> None:
    identity = Authenticator("none").authenticate("x", None)
    assert (identity.agent_id, identity.tenant, identity.authenticated) == ("x", None, False)
    with pytest.raises(AuthError):
        Authenticator("none").authenticate(None, None)


def _app(policy_file: Path, tmp_path: Path):
    settings = Settings(
        policy_file=policy_file, artifact_dir=tmp_path / "artifacts", plugins="none", auth_keys_file=_keys(tmp_path)
    )
    return create_app(settings)


def test_websocket_requires_a_valid_key(policy_file: Path, tmp_path: Path) -> None:
    client = TestClient(_app(policy_file, tmp_path))
    for headers in (
        {"x-statelock-agent-id": AGENT},
        {"x-statelock-agent-id": AGENT, "authorization": "Bearer slk_wrong"},
        {"x-statelock-agent-id": AGENT, "authorization": f"Bearer {OTHER_KEY}"},
    ):
        with client.websocket_connect("/statelock", headers=headers) as ws:
            message = ws.receive()
        assert message["code"] == 4401
        assert message["reason"] == "Statelock authentication failed"


def test_authenticated_but_unregistered_agent_is_refused(policy_file: Path, tmp_path: Path) -> None:
    client = TestClient(_app(policy_file, tmp_path))
    with client.websocket_connect("/statelock", headers={"authorization": f"Bearer {OTHER_KEY}"}) as ws:
        message = ws.receive()
    assert message["code"] == 4401
    assert "not registered" in message["reason"]


def test_violation_lookup_is_scoped_to_the_agent(policy_file: Path, tmp_path: Path) -> None:
    app = _app(policy_file, tmp_path)
    app.state.services.registry.record({"session_id": "s1", "agent_id": AGENT, "tenant_id": "acme", "rule": "r"})
    client = TestClient(app)
    unauthenticated = client.get("/violations/s1")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert client.get("/violations/s1", headers={"authorization": "Bearer slk_wrong"}).status_code == 401
    assert client.get("/violations/s1", headers={"authorization": f"Bearer {OTHER_KEY}"}).status_code == 404
    own = client.get("/violations/s1", headers={"authorization": f"Bearer {KEY}"})
    assert own.json()["rule"] == "r"
    assert client.get("/violations/none", headers={"authorization": f"Bearer {KEY}"}).status_code == 404


def test_cli_keygen_and_hash_key(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert main(["keygen", "--agent-id", AGENT, "--tenant", "acme"]) == 0
    captured = capsys.readouterr()
    key = captured.err.strip().splitlines()[-1]
    entry = yaml.safe_load(captured.out)["agents"][0]
    assert entry == {"agent_id": AGENT, "tenant": "acme", "key_sha256": hash_key(key)}
    assert key not in captured.out  # the key never goes to stdout

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(key + "\n"))
    assert main(["hash-key"]) == 0
    assert capsys.readouterr().out.strip() == hash_key(key)


def test_dev_keys_file_is_valid_and_matches_documented_keys() -> None:
    root = Path(__file__).resolve().parents[1]
    keys = load_keys_file(root / "docker" / "dev-keys.yaml")
    auth = Authenticator("api_key", keys)
    for agent in ("rogue_agent", "finance_reconciliation_agent", "portal_reconciliation_agent"):
        assert auth.authenticate(agent, f"Bearer slk_dev_{agent}").tenant == "dev"


def test_auditor_keys_are_their_own_role(tmp_path: Path) -> None:
    keys = KeysFile.model_validate(
        {
            "agents": [{"agent_id": AGENT, "tenant": "acme", "key_sha256": hash_key(KEY)}],
            "auditors": [{"auditor_id": "audit-team", "tenant": "acme", "key_sha256": hash_key("slk_audit")}],
        }
    )
    auth = Authenticator("api_key", keys)
    auditor = auth.authenticate_auditor("Bearer slk_audit")
    assert (auditor.auditor_id, auditor.tenant) == ("audit-team", "acme")
    assert auditor.may_read("acme")
    assert not auditor.may_read("globex")
    with pytest.raises(AuthError, match="agent key used as auditor key"):
        auth.authenticate_auditor(f"Bearer {KEY}")
    with pytest.raises(AuthError, match="auditor key used as agent key"):
        auth.authenticate(None, "Bearer slk_audit")
    with pytest.raises(ValidationError):  # one key, one role
        KeysFile.model_validate(
            {
                "agents": [{"agent_id": AGENT, "key_sha256": hash_key(KEY)}],
                "auditors": [{"auditor_id": "a", "key_sha256": hash_key(KEY)}],
            }
        )
    assert Authenticator("none").authenticate_auditor(None).may_read("anything")


def test_keygen_writes_an_auditor_entry(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keygen", "--auditor", "audit-team", "--tenant", "acme"]) == 0
    entry = yaml.safe_load(capsys.readouterr().out)["auditors"][0]
    assert entry["auditor_id"] == "audit-team"
    assert entry["tenant"] == "acme"


def test_keys_file_with_a_repeated_section_is_refused(tmp_path: Path) -> None:
    # What `keygen >> keys.yaml` twice produces: PyYAML alone would keep only the last block.
    path = tmp_path / "keys.yaml"
    first = yaml.safe_dump({"agents": [{"agent_id": AGENT, "key_sha256": hash_key(KEY)}]})
    second = yaml.safe_dump({"agents": [{"agent_id": "other_agent", "key_sha256": hash_key(OTHER_KEY)}]})
    path.write_text(first + second, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key 'agents'"):
        load_keys_file(path)


def test_keygen_append_adds_entries_under_their_sections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "keys.yaml"
    assert main(["keygen", "--agent-id", AGENT, "--tenant", "acme", "--append", str(path)]) == 0  # creates it
    path.write_text("# production keys\n" + path.read_text(encoding="utf-8") + "# end of agents\n", encoding="utf-8")
    path.chmod(0o640)
    assert main(["keygen", "--agent-id", "other_agent", "--tenant", "acme", "--append", str(path)]) == 0
    assert main(["keygen", "--reviewer", "alice", "--tenant", "acme", "--append", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing to redirect
    keys = [line for line in captured.err.splitlines() if line.startswith("slk_")]
    assert len(keys) == 3
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# production keys\nagents:\n")
    assert text.index("other_agent") < text.index("# end of agents")  # inside the agents list
    loaded = load_keys_file(path)
    assert [entry.agent_id for entry in loaded.agents] == [AGENT, "other_agent"]
    assert [entry.reviewer_id for entry in loaded.reviewers] == ["alice"]
    auth = Authenticator("api_key", loaded)
    assert auth.authenticate(None, f"Bearer {keys[1]}").agent_id == "other_agent"
    assert auth.authenticate_reviewer(f"Bearer {keys[2]}").reviewer_id == "alice"
    assert path.stat().st_mode & 0o777 == 0o640  # the file keeps its mode
    assert [p.name for p in tmp_path.iterdir()] == ["keys.yaml"]  # no temporary file left


def test_keygen_append_refuses_an_entry_the_file_cannot_take(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "keys.yaml"
    path.write_text("agents:\n  - {agent_id: a, tenant: acme, key_sha256: " + hash_key(KEY) + "}\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert main(["keygen", "--agent-id", "a", "--tenant", "globex", "--append", str(path)]) == 1  # two tenants
    assert "listed under two tenants" in capsys.readouterr().err
    path.write_text("agents: []\n", encoding="utf-8")
    assert main(["keygen", "--agent-id", "a", "--append", str(path)]) == 1  # a flow list
    assert "block list" in capsys.readouterr().err
    path.write_text(before + "agents: []\n", encoding="utf-8")
    assert main(["keygen", "--agent-id", "b", "--append", str(path)]) == 1  # already broken
    err = capsys.readouterr().err
    assert "duplicate key 'agents'" in err
    assert "slk_" not in err  # no key is shown for an entry that was not stored
