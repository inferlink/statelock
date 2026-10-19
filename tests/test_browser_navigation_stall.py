"""A page load Chromium never pauses for the guard is stopped, so the agent's navigation
fails fast (and can be retried) instead of hanging."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

from browser_support import chromium_available, run_session, running_server, site_router  # noqa: E402
from fastapi import APIRouter  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from statelock.proxy.guard import DOCUMENT, PageGuard  # noqa: E402

pytestmark = pytest.mark.browser
STALL_TIMEOUT = 1.0


@pytest.fixture(scope="module")
def stalling_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    slow = APIRouter()

    @slow.get("/slow")
    async def slow_page() -> HTMLResponse:
        await asyncio.sleep(STALL_TIMEOUT * 2.5)
        return HTMLResponse("<title>Slow</title>slow")

    with running_server(
        tmp_path_factory, routers=(site_router(), slow), navigation_stall_timeout=STALL_TIMEOUT
    ) as running:
        yield running


@pytest.fixture
def first_page_load_never_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard never sees the first page load's pause: as when Chromium does not send it."""
    original = PageGuard._on_request_paused
    seen = {"documents": 0}

    def drop_first_document(self: PageGuard, session_id: str, state: Any, params: dict[str, Any]) -> None:
        if params.get("resourceType") == DOCUMENT and "/gov/" in str(params.get("request", {}).get("url")):
            seen["documents"] += 1
            if seen["documents"] == 1:
                return
        original(self, session_id, state, params)

    monkeypatch.setattr(PageGuard, "_on_request_paused", drop_first_document)


@pytest.mark.usefixtures("first_page_load_never_paused")
def test_a_stalled_page_load_fails_fast_and_a_retry_loads(stalling_server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        started = time.monotonic()
        try:
            await page.goto(stalling_server["base"] + "/gov/form", timeout=30_000)
        except playwright_api.Error as error:
            failed = (str(error).splitlines()[0], time.monotonic() - started)
        else:
            failed = None
        await page.goto(stalling_server["base"] + "/gov/form", timeout=30_000)
        return failed, await page.title()

    _, (failed, title) = run_session(stalling_server, "flow_agent", "", steps)
    assert failed is not None, "the stalled load should have been stopped"
    message, seconds = failed
    assert seconds < 10, message
    assert title == "Form"


def test_a_slow_site_is_not_mistaken_for_a_stalled_load(stalling_server: dict[str, Any]) -> None:
    # Chromium pauses the load for the guard at once; the site then takes longer than the timeout.
    async def steps(page: Any) -> Any:
        await page.goto(stalling_server["base"] + "/slow", timeout=30_000)
        return await page.title()

    _, title = run_session(stalling_server, "flow_agent", "", steps)
    assert title == "Slow"
