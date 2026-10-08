from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from statelock import __main__ as cli
from statelock.app import create_app
from statelock.plugins import PluginContext, discover_plugins, setup_plugins
from statelock.services import Services
from statelock.settings import Settings


def _settings(policy_file: Path, tmp_path: Path, **overrides) -> Settings:
    overrides.setdefault("auth_mode", "none")
    return Settings(policy_file=policy_file, artifact_dir=tmp_path / "artifacts", plugins="none", **overrides)


def test_missing_policy_file_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        create_app(_settings(tmp_path / "missing.yaml", tmp_path))


def test_core_routes(policy_file: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(policy_file, tmp_path)))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/violations/unknown").status_code == 404
    assert client.get("/demo/finance").status_code == 404
    assert client.get("/debug/artifacts").status_code == 404


def test_demo_routes_are_opt_in(policy_file: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(policy_file, tmp_path, demo=True, debug=True)))
    page = client.get("/demo/finance?scenario=mismatch").text
    assert 'data-statelock-key="bank_deposit_amount"' in page
    assert "$4,900.00" in page
    # Debug mode adds no routes: the artifact listing would show every tenant's sessions.
    assert client.get("/debug/artifacts").status_code == 404


def test_violation_lookup(policy_file: Path, tmp_path: Path) -> None:
    app = create_app(_settings(policy_file, tmp_path))
    app.state.services.registry.record({"session_id": "abc", "rule": "r"})
    assert TestClient(app).get("/violations/abc").json()["rule"] == "r"


def test_unregistered_agent_is_refused(policy_file: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(policy_file, tmp_path)))
    with client.websocket_connect("/statelock", headers={"x-statelock-agent-id": "nobody"}) as ws:
        message = ws.receive()
    assert message["type"] == "websocket.close"
    assert message["code"] == 4401


def test_invalid_session_id_is_refused(policy_file: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(policy_file, tmp_path)))
    headers = {"x-statelock-agent-id": "finance_reconciliation_agent", "x-statelock-session-id": "nope"}
    with client.websocket_connect("/statelock", headers=headers) as ws:
        message = ws.receive()
    assert message["code"] == 4409


def test_a_running_sessions_id_is_refused(policy_file: Path, tmp_path: Path) -> None:
    app = create_app(_settings(policy_file, tmp_path))
    session_id = "11111111-2222-3333-4444-555555555555"
    app.state.services.active_sessions.add(session_id)  # as while that session runs (before any record)
    headers = {"x-statelock-agent-id": "finance_reconciliation_agent", "x-statelock-session-id": session_id}
    with TestClient(app).websocket_connect("/statelock", headers=headers) as ws:
        message = ws.receive()
    assert (message["code"], "already used" in message["reason"]) == (4409, True)


def test_plugin_can_wrap_sink_and_add_routes(policy_file: Path, tmp_path: Path) -> None:
    settings = _settings(policy_file, tmp_path)
    services = Services.from_settings(settings)
    original_sink = services.sink

    class Plugin:
        name = "test"

        def setup(self, ctx: PluginContext) -> None:
            ctx.services.sink = ("wrapped", ctx.services.sink)  # type: ignore[assignment]

            @ctx.app.get("/plugin")
            async def plugin_route() -> dict[str, str]:
                return {"ok": "yes"}

    app = create_app(settings, services)
    setup_plugins(PluginContext(app=app, services=services), [Plugin()])
    assert services.sink == ("wrapped", original_sink)
    assert TestClient(app).get("/plugin").json() == {"ok": "yes"}


def test_discover_plugins_selection() -> None:
    assert discover_plugins("none") == []
    with pytest.raises(RuntimeError, match="not installed"):
        discover_plugins("definitely_missing_plugin")


def test_cli_serves_on_the_documented_port_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))
    assert cli.main([]) == 0
    assert (calls[0]["host"], calls[0]["port"]) == ("127.0.0.1", 8010)
    assert cli.main(["--host", "0.0.0.0", "--port", "9000"]) == 0  # noqa: S104 - an argument, not a bind
    assert (calls[1]["host"], calls[1]["port"]) == ("0.0.0.0", 9000)  # noqa: S104


def test_cli_help_describes_host_and_port(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert "address the proxy listens on" in help_text and "port the proxy listens on (default: 8010)" in help_text


def test_startup_settings_for_chromium_and_requests_are_bounded(policy_file: Path, tmp_path: Path) -> None:
    services = Services.from_settings(
        _settings(policy_file, tmp_path, chromium_start_timeout=3, max_concurrent_requests=2)
    )
    assert services.launcher.start_timeout == 3
    with pytest.raises(ValueError, match="max_concurrent_requests"):
        Settings(max_concurrent_requests=0)
