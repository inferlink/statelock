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

from statelock.proxy.bridge import STALLED_CLOSE_REASON  # noqa: E402
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
def test_a_stalled_page_load_ends_the_session_fast_and_a_new_session_loads(stalling_server: dict[str, Any]) -> None:
    async def steps(page: Any) -> Any:
        started = time.monotonic()
        with pytest.raises(playwright_api.Error) as caught:
            await page.goto(stalling_server["base"] + "/gov/form", timeout=30_000)
        return str(caught.value), time.monotonic() - started

    _, (error, seconds) = run_session(stalling_server, "flow_agent", "", steps)
    assert STALLED_CLOSE_REASON in error
    assert seconds < 10
    # A new session has a new browser; this one's first page load is paused as usual.
    _, title = run_session(stalling_server, "flow_agent", "/gov/form", lambda page: page.title())
    assert title == "Form"


def test_a_slow_site_is_not_mistaken_for_a_stalled_load(stalling_server: dict[str, Any]) -> None:
    # Chromium pauses the load for the guard at once; the site then takes longer than the timeout.
    async def steps(page: Any) -> Any:
        await page.goto(stalling_server["base"] + "/slow", timeout=30_000)
        return await page.title()

    _, title = run_session(stalling_server, "flow_agent", "", steps)
    assert title == "Slow"


@pytest.mark.usefixtures("first_page_load_never_paused")
def test_the_browser_tests_start_a_new_session_when_the_first_load_stalls(
    stalling_server: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    _, title = run_session(stalling_server, "flow_agent", "/gov/form", lambda page: page.title())
    assert title == "Form"
    # The proxy log says what Chromium was doing, for the next real occurrence.
    assert any("Chromium stopped loading pages. Processes:" in r.getMessage() for r in caplog.records)
