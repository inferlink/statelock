"""Secrets file and placeholder injection (no browser)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from statelock.credentials import InjectionError, SecretScrubber, SecretStore
from statelock.services import Services
from statelock.settings import Settings


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **entry: object) -> SecretStore:
    monkeypatch.setenv("PORTAL_PW", 'p"a\\ss-é')
    path = tmp_path / "secrets.yaml"
    base = {"name": "pw", "agents": ["agent"], "url_contains": "https://x.test/login", "value_env": "PORTAL_PW"}
    path.write_text(yaml.safe_dump({"secrets": [{**base, **entry}]}), encoding="utf-8")
    return SecretStore.load(path)


def test_inject_replaces_placeholders_where_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path, monkeypatch)
    text, names = store.inject("{{secret:pw}}", agent_id="agent", url="https://x.test/login", password_field=True)
    assert (text, names) == ('p"a\\ss-é', ["pw"])
    for kwargs, message in [
        ({"agent_id": "other", "url": "https://x.test/login", "password_field": True}, "may not use"),
        ({"agent_id": "agent", "url": "https://x.test/home", "password_field": True}, "only be typed on pages"),
        ({"agent_id": "agent", "url": "https://x.test/loginfoo", "password_field": True}, "only be typed"),
        # Another host, even one whose name or path contains the scope.
        ({"agent_id": "agent", "url": "https://evil.test/login", "password_field": True}, "only be typed"),
        ({"agent_id": "agent", "url": "https://x.test.evil.test/login", "password_field": True}, "only be typed"),
        ({"agent_id": "agent", "url": "https://x.test@evil.test/login", "password_field": True}, "only be typed"),
        ({"agent_id": "agent", "url": "https://evil.test/https://x.test/login", "password_field": True}, "only"),
        ({"agent_id": "agent", "url": "http://x.test/login", "password_field": True}, "only be typed"),
        # The query and fragment are chosen by whoever links the page: they never match.
        ({"agent_id": "agent", "url": "https://evil.test/?next=/login", "password_field": True}, "only be typed"),
        ({"agent_id": "agent", "url": "https://evil.test/#/login", "password_field": True}, "only be typed"),
        ({"agent_id": "agent", "url": "https://x.test/login", "password_field": False}, "password field"),
    ]:
        with pytest.raises(InjectionError, match=message):
            store.inject("{{secret:pw}}", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(InjectionError, match="unknown secret"):
        store.inject("{{secret:nope}}", agent_id="agent", url="https://x.test/login", password_field=True)
    assert 'p"a' not in repr(store._by_name["pw"])


def test_non_password_fields_can_be_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path, monkeypatch, password_fields_only=False)
    url = "https://X.test:443/app/login?next=/home"
    assert store.inject("id {{secret:pw}}", agent_id="agent", url=url, password_field=False)[1] == ["pw"]


@pytest.mark.parametrize(
    "scope",
    [
        "/index.php/journal/login",
        "x.test/login",
        "ftp://x.test/",
        "https:///login",
        "https://u@x.test/",
        "https://x.test/?a=1",
    ],
)
def test_secret_scope_must_start_with_an_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    with pytest.raises(ValidationError, match="must start with the origin"):
        _store(tmp_path, monkeypatch, url_contains=scope)


def test_secrets_file_rejects_duplicate_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAL_PW", "x")
    path = tmp_path / "secrets.yaml"
    entry = "  - {name: pw, agents: [agent], url_contains: 'https://x.test/', value_env: PORTAL_PW}\n"
    path.write_text(f"secrets:\n{entry}secrets:\n{entry}", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key 'secrets'"):
        SecretStore.load(path)


def test_secrets_file_is_validated_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTAL_PW", raising=False)
    path = tmp_path / "secrets.yaml"
    entry = {"name": "pw", "agents": ["agent"], "url_contains": "https://x.test/login", "value_env": "PORTAL_PW"}
    path.write_text(yaml.safe_dump({"secrets": [entry]}), encoding="utf-8")
    with pytest.raises(ValueError, match="PORTAL_PW is not set"):
        SecretStore.load(path)
    path.write_text(yaml.safe_dump({"secrets": [{**entry, "value_file": "/x"}]}), encoding="utf-8")
    with pytest.raises(ValidationError, match="exactly one"):
        SecretStore.load(path)
    with pytest.raises(FileNotFoundError):
        Services.from_settings(Settings(secrets_file=tmp_path / "missing.yaml", auth_mode="none"))


def test_scrubber_removes_raw_and_json_escaped_values() -> None:
    scrubber = SecretScrubber()
    assert not scrubber
    scrubber.add('p"a\\ss-é')
    message = json.dumps({"result": {"value": 'p"a\\ss-é'}})
    assert 'p\\"a' not in scrubber.text(message)
    assert json.loads(scrubber.text(message))["result"]["value"] == "[SECRET]"
    ascii_message = json.dumps({"v": 'p"a\\ss-é'}, ensure_ascii=True)
    assert json.loads(scrubber.text(ascii_message))["v"] == "[SECRET]"
    assert scrubber.data({"a": ['x p"a\\ss-é y']}) == {"a": ["x [SECRET] y"]}


def test_dev_secrets_file_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[1] / "docker" / "dev-secrets.yaml"
    if not path.exists():  # the Docker test image mounts only some files
        pytest.skip("docker/dev-secrets.yaml is not available")
    monkeypatch.setenv("OJS_PASSWORD", "mock-editor-pass")
    assert SecretStore.load(path).names == ["ojs_password"]
