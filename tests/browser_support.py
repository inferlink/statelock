"""Shared setup for the browser integration tests (test_browser_*.py).

The ``server`` fixture (conftest.py) starts a Statelock proxy with a test site,
authentication on, and a policy per test agent. Skipped when Playwright or its
Chromium is not installed.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

import uvicorn  # noqa: E402
import yaml  # noqa: E402
from fastapi import APIRouter, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402

from statelock.app import create_app, get_services  # noqa: E402
from statelock.auth import hash_key  # noqa: E402
from statelock.client import StatelockPolicyViolationError, connect_statelock  # noqa: E402
from statelock.settings import Settings  # noqa: E402

# Authentication is on for every browser test: each agent connects with its own key.
AGENTS = (
    "finance_reconciliation_agent",
    "post_fail_agent",
    "flow_agent",
    "portal_reconciliation_agent",
    "review_agent",
    "cookie_agent",
    "secret_agent",
    "request_agent",
)
# The secret the secrets file gives secret_agent on /gov/signin (credential injection tests).
PORTAL_PASSWORD = "test-password-not-secret-$$"  # noqa: S105 - a fake value
API_TOKEN = "test-token-not-secret"  # noqa: S105 - a fake value for a secret request header
REVIEWER_KEY = "slk_test_reviewer"


def agent_key(agent: str) -> str:
    return f"slk_test_{agent}"


POLICY = """
policies:
  - agent_id: finance_reconciliation_agent
    target_url_contains: /demo/finance
    pre_conditions:
      - assert_field_equal: {left: bank_deposit_amount, right: erp_invoice_amount}
    post_conditions:
      - trigger: {click_text: [Mark as Paid]}
        require_page_text: {values: [Reconciliation complete]}
  - agent_id: post_fail_agent
    target_url_contains: /demo/finance
    post_conditions:
      - trigger: {key: [Enter]}
        require_page_text: {values: [Text that never appears]}
  - agent_id: flow_agent
    target_url_contains: /gov/
    pre_conditions:
      - restrict_downloads: {extensions: [.pdf, .bin]}
  - agent_id: portal_reconciliation_agent
    target_url_contains: /demo/erp
    fields:
      - {name: bank_deposit, url_contains: /demo/bank, allowed_origins: ["http://127.0.0.1:__PORT__"], selector: "#deposit-amount", remember: true}
      - {name: erp_invoice, selector: "#invoice-amount"}
    pre_conditions:
      - assert_field_equal: {left: remembered.bank_deposit, right: erp_invoice}
      - assert_compare: {left: erp_invoice, op: "<=", value: 10000}
      - restrict_uploads: {extensions: [.pdf], max_bytes: 5000000, max_files: 1}
    post_conditions:
      - trigger: {click_text: [Mark as Paid]}
        require_page_text: {values: [Reconciliation complete]}
  - agent_id: review_agent
    target_url_contains: /gov/approve
    fields:
      - {name: amount, selector: "#amount"}
    pre_conditions:
      - assert_compare: {left: amount, op: "<=", value: 1000, on_fail: review}
      - prohibit_click_text: {values: [Delete]}
  - agent_id: cookie_agent
    target_url_contains: /gov/
    cookie_access: {names: [csrftoken], domains: [cdn.example.test]}
  - agent_id: secret_agent
    target_url_contains: /gov/
  - agent_id: request_agent
    target_url_contains: /gov/
    request_access:
      - url_pattern: '^http://127\\.0\\.0\\.1:[0-9]+/gov/me$'
      - url_pattern: '^http://127\\.0\\.0\\.1:[0-9]+/gov/echo$'
        methods: [POST]
      - url_pattern: '^http://127\\.0\\.0\\.1:[0-9]+/gov/large$'
        max_response_bytes: 200000
      - url_pattern: '^http://127\\.0\\.0\\.1:[0-9]+/gov/redirect-(me|away)$'
      - url_pattern: '^http://127\\.0\\.0\\.1:[0-9]+/gov/redirect-plain$'
"""

SITE = {
    "/gov/form": """<html><head><title>Form</title></head><body>
<form id=f action="/gov/result" method="post"><input id=q name=q><button id=go type=submit>Submit</button></form>
<script>window.app={save(){return fetch('/gov/api/save',{method:'POST'})}}</script></body></html>""",
    "/gov/result": """<html><head><title>Result</title></head><body><p>Submitted</p>
<a id=next href="/gov/step3">Next step</a>
<button id=later onclick="setTimeout(()=>location.href='/gov/redirected',600)">Redirect later</button>
<script>fetch('/gov/api/ping',{method:'POST'})</script></body></html>""",
    "/gov/step3": "<html><head><title>Step3</title></head><body>Step 3</body></html>",
    "/gov/redirected": "<html><head><title>Redirected</title></head><body>Redirected</body></html>",
    "/gov/upload": """<html><head><title>Upload</title></head><body>
<form id=uf action="/gov/result" method="post" enctype="multipart/form-data">
<input id=file type=file name=file multiple><button id=upgo type=submit>Upload</button></form><p id=picked>none</p>
<script>document.getElementById('file').addEventListener('change',
  (e) => { document.getElementById('picked').textContent = 'picked:' + e.target.files.length })</script></body></html>""",
    "/gov/approve": """<html><head><title>Approve</title></head><body>
<p>Amount: <span id=amount>$5,000.00</span></p><p id=state>open</p>
<button id=pay onclick="document.getElementById('state').textContent='paid'">Pay</button>
<button id=del>Delete</button>
<script>if (location.search.includes('drift'))
  setTimeout(() => { document.getElementById('amount').textContent = '$9,000.00' }, 1500)</script>
</body></html>""",
    "/gov/relay": """<html><head><title>Relay</title></head><body>
<button id=relay onclick="document.getElementById('hidden-go').click()">Relay</button>
<button id=hidden-go onclick="document.title='relayed'">Target</button>
<div style="position:relative"><button id=covered onclick="document.title='covered'">Covered</button>
<div style="position:absolute;inset:0;background:#fff"></div></div>
</body></html>""",
    "/gov/login": """<html><head><title>Login</title></head><body>
<input id=user autocomplete=username><input id=pw type=password><input id=otp autocomplete=one-time-code>
</body></html>""",
    "/gov/docs": """<html><head><title>Docs</title></head><body>
<a id=pdf href="/gov/files/report.pdf">Download report</a>
<a id=exe href="/gov/files/tool.exe">Download tool</a>
<a id=big href="/gov/files/big.bin">Download archive</a>
</body></html>""",
    "/gov/drag": """<html><head><title>Drag</title></head><body>
<div id=item draggable=true style="width:80px;height:40px">Item</div>
<div id=zone style="width:200px;height:100px;margin-top:40px">Drop here</div><p id=dropstatus>waiting</p>
<script>const zone = document.getElementById('zone');
zone.addEventListener('dragover', (e) => e.preventDefault());
zone.addEventListener('drop', (e) => { e.preventDefault(); document.getElementById('dropstatus').textContent = 'dropped' })
</script></body></html>""",
}


FILES = {
    "report.pdf": b"%PDF-1.4 quarterly report " * 200,
    "tool.exe": b"MZ not a real program",
    "big.bin": b"x" * 300_000,
}
DOWNLOAD_LIMIT = 200_000


def site_router() -> APIRouter:
    router = APIRouter()

    def page(path: str) -> Any:
        async def handler() -> HTMLResponse:
            return HTMLResponse(SITE[path])

        return handler

    for path in SITE:
        router.add_api_route(path, page(path), methods=["GET", "POST"])

    async def api() -> JSONResponse:
        return JSONResponse({})

    router.add_api_route("/gov/api/{name}", api, methods=["GET", "POST"])

    async def set_cookies() -> HTMLResponse:
        response = HTMLResponse("<html><head><title>Cookies</title></head><body>set</body></html>")
        response.set_cookie("session", "secret-session", httponly=True)
        response.set_cookie("csrftoken", "csrf-123")
        return response

    router.add_api_route("/gov/cookies", set_cookies, methods=["GET"])

    async def login_page() -> HTMLResponse:
        return HTMLResponse(
            "<html><head><title>Login</title></head><body><form id=login method=post action=/gov/signin-check>"
            "<input id=username name=username><input id=password name=password type=password>"
            "<button id=submit type=submit>Log in</button></form></body></html>"
        )

    async def login_check(request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode())
        ok = form.get("password", [""])[0] == PORTAL_PASSWORD
        return HTMLResponse(f"<html><head><title>{'Welcome' if ok else 'Denied'}</title></head><body></body></html>")

    async def me(request: Request) -> JSONResponse:
        return JSONResponse({"user": "signed-in" if request.cookies.get("session") == "secret-session" else None})

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "body": (await request.body()).decode(),
                "content_type": request.headers.get("content-type"),
                "token_ok": request.headers.get("x-api-token") == API_TOKEN,
                "token": request.headers.get("x-api-token"),  # an endpoint that echoes it back
            }
        )

    def redirect(to: str) -> Callable[[], Awaitable[Response]]:
        async def handler() -> Response:
            return Response(status_code=302, headers={"location": to})

        return handler

    async def large() -> Response:
        return Response(b"x" * 300_000, media_type="application/octet-stream")

    router.add_api_route("/gov/me", me, methods=["GET"])
    router.add_api_route("/gov/echo", echo, methods=["POST"])
    router.add_api_route("/gov/large", large, methods=["GET"])
    for name, to in (("me", "/gov/me"), ("away", "/gov/form"), ("plain", "/gov/me")):
        router.add_api_route(f"/gov/redirect-{name}", redirect(to), methods=["GET"])
    router.add_api_route("/gov/signin", login_page, methods=["GET"])
    router.add_api_route("/gov/signin-check", login_check, methods=["POST"])

    async def file(name: str) -> Response:
        return Response(
            FILES[name],
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    router.add_api_route("/gov/files/{name}", file, methods=["GET"])
    return router


def chromium_available() -> bool:
    async def probe() -> bool:
        async with playwright_api.async_playwright() as p:
            return Path(p.chromium.executable_path).exists()

    try:
        return asyncio.run(probe())
    except Exception:
        return False


FIRST_LOAD_TIMEOUT_MS = 90_000


async def _first_load(page: Any, url: str) -> None:
    """The first page.goto of a session. On a timeout, say whether the test site still
    answers directly, so a CI failure shows whether the server or the browser path stalled."""
    try:
        await page.goto(url, timeout=FIRST_LOAD_TIMEOUT_MS)
    except playwright_api.TimeoutError as error:
        direct = await asyncio.to_thread(_probe, url)
        raise AssertionError(
            f"first load of {url} timed out through Statelock; {direct}; page.url={page.url}"
        ) from error


def _probe(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - the local test site
            return f"the site answers directly ({response.status})"
    except OSError as error:
        return f"the site does not answer directly either ({error})"


async def agent_session(server: dict[str, Any], agent: str, start: str, steps: Any) -> tuple[str, Any]:
    async with playwright_api.async_playwright() as p:
        conn = await connect_statelock(p, server["ws"], agent, api_key=agent_key(agent))
        page = conn.browser.contexts[0].pages[0]
        try:
            async with conn.guard():
                # The first load waits for Chromium to start inside the proxy, which can take
                # longer than Playwright's 30 s default on a busy Docker host.
                await _first_load(page, server["base"] + start)
                result = await steps(page)
            return conn.session_id, result
        except StatelockPolicyViolationError as violation:
            return conn.session_id, violation
        finally:
            if conn.browser.is_connected():
                await conn.browser.close()


def run_session(server: dict[str, Any], agent: str, start: str, steps: Any) -> tuple[str, Any]:
    return asyncio.run(agent_session(server, agent, start, steps))


async def wait_for_proxy_close(page: Any, timeout: float = 5.0) -> None:
    """Wait for a blocked session to disconnect instead of guessing a fixed delay."""
    browser = page.context.browser
    closed = asyncio.Event()

    def on_disconnect(_: Any) -> None:
        closed.set()

    browser.on("disconnected", on_disconnect)
    if browser.is_connected():
        with suppress(TimeoutError):
            await asyncio.wait_for(closed.wait(), timeout)
    assert not browser.is_connected(), "blocked session did not close"


def network_log(server: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    path = server["root"] / "artifacts" / "sessions" / session_id / "network.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def actions(server: dict[str, Any], session_id: str, method: str | None = None) -> list[dict[str, Any]]:
    """Stored action records. Guard violations are recorded just after the session closes,
    so when ``method`` is given, wait until a record with that method exists."""
    session_dir = server["root"] / "artifacts" / "sessions" / session_id
    deadline = time.time() + 5
    while True:
        records = [
            json.loads((d / "context.json").read_text())
            for d in sorted(session_dir.glob("action-*"))
            if (d / "context.json").exists()
        ]
        if method is None or any(r["context"]["method"] == method for r in records) or time.time() > deadline:
            return records
        time.sleep(0.05)


async def upload_session(server: dict[str, Any], steps: Any) -> tuple[str, Any]:
    async with playwright_api.async_playwright() as p:
        conn = await connect_statelock(p, server["ws"], "flow_agent", api_key=agent_key("flow_agent"))
        page = conn.browser.contexts[0].pages[0]
        try:
            async with conn.guard():
                await page.goto(server["base"] + "/gov/upload")
                return conn.session_id, await steps(conn, page)
        except StatelockPolicyViolationError as violation:
            return conn.session_id, violation
        finally:
            if conn.browser.is_connected():
                await conn.browser.close()


async def portal_session(server: dict[str, Any], scenario: str, upload_name: str = "remittance.pdf") -> tuple[str, Any]:
    async with playwright_api.async_playwright() as p:
        conn = await connect_statelock(
            p, server["ws"], "portal_reconciliation_agent", api_key=agent_key("portal_reconciliation_agent")
        )
        page = conn.browser.contexts[0].pages[0]
        try:
            async with conn.guard():
                await page.goto(f"{server['base']}/demo/bank?scenario={scenario}")
                await page.inner_text("#deposit-amount")  # read only: no governed action on the bank page
                await page.goto(f"{server['base']}/demo/erp?scenario={scenario}")
                remittance = {"name": upload_name, "mimeType": "application/pdf", "buffer": b"%PDF-1.4"}
                await conn.set_input_files(page, "#remittance", [remittance])
                await page.click("text=Mark as Paid")
                await page.wait_for_selector("text=Reconciliation complete", timeout=2000)
                return conn.session_id, "completed"
        except StatelockPolicyViolationError as violation:
            return conn.session_id, violation
        finally:
            if conn.browser.is_connected():
                await conn.browser.close()


async def download_session(server: dict[str, Any], steps: Any) -> tuple[str, Any]:
    async with playwright_api.async_playwright() as p:
        conn = await connect_statelock(p, server["ws"], "flow_agent", api_key=agent_key("flow_agent"))
        page = conn.browser.contexts[0].pages[0]
        try:
            async with conn.guard():
                await page.goto(server["base"] + "/gov/docs")
                return conn.session_id, await steps(conn, page)
        except StatelockPolicyViolationError as violation:
            return conn.session_id, violation
        finally:
            if conn.browser.is_connected():
                await conn.browser.close()


# (name, agents, path, value) of each secret in the proxy's secrets file; the
# test server's origin is put before the path (url_contains).
Secret = tuple[str, list[str], str, str]
PORTAL_SECRETS: tuple[Secret, ...] = (
    ("portal_password", ["secret_agent"], "/gov/signin", PORTAL_PASSWORD),
    ("api_token", ["request_agent"], "/gov/echo", API_TOKEN),
)


def _secrets_file(root: Path, origin: str, secrets: tuple[Secret, ...]) -> Path:
    entries = []
    for name, agents, path, value in secrets:
        value_file = root / f"secret-{name}"
        value_file.write_text(value + "\n", encoding="utf-8")
        entry = {"name": name, "agents": agents, "url_contains": origin + path, "value_file": str(value_file)}
        if name == "api_token":
            entry["password_fields_only"] = False  # a request header, not a password field
        entries.append(entry)
    path = root / "secrets.yaml"
    path.write_text(yaml.safe_dump({"secrets": entries}), encoding="utf-8")
    return path


@contextmanager
def running_server(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    policy: str = POLICY,
    agents: tuple[str, ...] = AGENTS,
    routers: tuple[APIRouter, ...] | None = None,
    secrets: tuple[Secret, ...] | None = None,
    perception: Any = None,
    rule_modules: str = "",
) -> Iterator[dict[str, Any]]:
    """Start the proxy with a site (default: the test site); yields {base, ws, root, app}."""
    root = tmp_path_factory.mktemp("statelock")
    policy_path = root / "policy.yaml"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    policy_path.write_text(policy.replace("__PORT__", str(port)), encoding="utf-8")
    keys = root / "keys.yaml"
    keys.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {"agent_id": agent, "tenant": "test", "key_sha256": hash_key(agent_key(agent))} for agent in agents
                ],
                "reviewers": [{"reviewer_id": "test_reviewer", "tenant": "test", "key_sha256": hash_key(REVIEWER_KEY)}],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        policy_file=policy_path,
        artifact_dir=root / "artifacts",
        demo=True,
        plugins="none",
        auth_keys_file=keys,
        download_max_file_bytes=DOWNLOAD_LIMIT,
        saved_sessions_key_file=root / "keys" / "saved-sessions.key",
        secrets_file=_secrets_file(
            root, f"http://127.0.0.1:{port}", secrets if secrets is not None else PORTAL_SECRETS
        ),
        rule_modules=rule_modules,
    )
    app = create_app(settings)
    if perception is not None:  # a fake vision-language model (visual_assert)
        get_services(app).evaluator.perception = perception
    for router in routers if routers is not None else (site_router(),):
        app.include_router(router)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not uv.started:
        raise RuntimeError("the Statelock test server did not start within 10 s")
    try:
        yield {"base": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}/statelock", "root": root, "app": app}
    finally:
        uv.should_exit = True
        thread.join(timeout=10)
