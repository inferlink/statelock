"""Saved browser sessions in a real browser: restore, save at a clean end, no save after a violation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from browser_support import agent_key, chromium_available, running_server, site_router  # noqa: E402
from fastapi import APIRouter, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from statelock import events as event_names  # noqa: E402
from statelock.client import create_session_url  # noqa: E402
from statelock.saved_sessions import SavedSessionKey  # noqa: E402

pytestmark = pytest.mark.browser

AGENT = "flow_agent"  # restrict_downloads allows .pdf and .bin on /gov/ pages
NAME = "portal-login"


def _login_router() -> APIRouter:
    router = APIRouter()

    async def login(value: str) -> HTMLResponse:
        # A login: an HttpOnly session cookie and a localStorage marker.
        response = HTMLResponse(
            f"<html><body><script>localStorage.setItem('marker', {value!r})</script>logged in</body></html>"
        )
        response.set_cookie("sid", value, httponly=True)
        return response

    async def show(request: Request) -> HTMLResponse:
        cookie = request.cookies.get("sid", "none")
        return HTMLResponse(
            f"<html><body><p id=cookie>{cookie}</p><p id=marker></p>"
            "<script>marker.textContent = localStorage.getItem('marker') || 'none'</script></body></html>"
        )

    router.add_api_route("/app/login", login, methods=["GET"])
    router.add_api_route("/app/show", show, methods=["GET"])
    return router


@pytest.fixture(scope="module")
def saved_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    with running_server(tmp_path_factory, routers=(site_router(), _login_router())) as running:
        closed: list[dict[str, Any]] = []

        async def on_closed(payload: dict[str, Any]) -> None:
            closed.append(payload)

        running["app"].state.services.events.subscribe(event_names.SESSION_CLOSED, on_closed)
        running["closed"] = closed
        yield running


def _wait_closed(server: dict[str, Any], session_id: str) -> dict[str, Any]:
    deadline = time.time() + 15
    while time.time() < deadline:
        for payload in server["closed"]:
            if payload["session_id"] == session_id:
                return payload
        time.sleep(0.05)
    raise AssertionError(f"session {session_id} did not close")


def _run(server: dict[str, Any], steps: Any, *, save: bool) -> tuple[str, Any]:
    url = create_session_url(server["base"], api_key=agent_key(AGENT), saved_session=NAME, save_session=save)

    async def main() -> Any:
        async with playwright_api.async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(url.cdp_url)
            try:
                return await steps(browser.contexts[0].pages[0])
            finally:
                if browser.is_connected():
                    await browser.close()

    result = asyncio.run(main())
    _wait_closed(server, url.session_id)
    return url.session_id, result


def _show(server: dict[str, Any]) -> Any:
    async def steps(page: Any) -> tuple[str, str]:
        await page.goto(server["base"] + "/app/show")
        await page.wait_for_function("document.querySelector('#marker').textContent !== ''")
        return str(await page.inner_text("#cookie")), str(await page.inner_text("#marker"))

    return steps


def test_saved_session_restores_cookies_and_local_storage(saved_server: dict[str, Any]) -> None:
    services = saved_server["app"].state.services
    key = SavedSessionKey("test", AGENT, NAME)

    async def log_in(page: Any) -> None:
        await page.goto(saved_server["base"] + "/app/login?value=first")

    _run(saved_server, log_in, save=True)
    assert services.saved_sessions.names(tenant_id="test", agent_id=AGENT) == [NAME]
    raw = next((saved_server["root"] / "artifacts" / "saved-sessions").rglob("*.slsession")).read_text()
    assert "first" not in raw  # encrypted at rest

    # A new session with the saved name starts logged in: the cookie and localStorage are back.
    assert _run(saved_server, _show(saved_server), save=False)[1] == ("first", "first")

    # A session that ends on a violation does not overwrite the saved state.
    async def change_then_violate(page: Any) -> None:
        await page.goto(saved_server["base"] + "/app/login?value=changed")
        await page.goto(saved_server["base"] + "/gov/docs")
        try:
            await page.click("#exe")  # a blocked download ends the session
            await page.wait_for_event("close", timeout=5000)
        except playwright_api.Error:
            pass

    session_id, _ = _run(saved_server, change_then_violate, save=True)
    assert _wait_closed(saved_server, session_id)["violation"] is True
    assert services.saved_sessions.load_payload(key)["cookies"][0]["value"] == "first"
    assert _run(saved_server, _show(saved_server), save=False)[1] == ("first", "first")
