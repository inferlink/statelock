"""Unchanged agent code through Statelock: session URLs and Playwright's own file APIs."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

playwright_api = pytest.importorskip("playwright.async_api")
playwright_sync_api = pytest.importorskip("playwright.sync_api")

import websockets  # noqa: E402
from browser_support import (  # noqa: E402
    FILES,
    StatelockPolicyViolationError,
    actions,
    agent_key,
    chromium_available,
)

import statelock.client  # noqa: E402
from statelock.client import create_session_url, statelock_session  # noqa: E402

pytestmark = pytest.mark.browser

AGENT = "flow_agent"


@pytest.fixture
def installed() -> Iterator[None]:
    statelock.client.install()
    yield
    statelock.client.uninstall()


def _session_url(server: dict[str, Any]) -> Any:
    return create_session_url(server["base"], api_key=agent_key(AGENT))


async def _with_page(url: str, steps: Any) -> Any:
    """What an agent framework does with a browser URL: connect_over_cdp, no headers."""
    async with playwright_api.async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(url)
        try:
            return await steps(browser.contexts[0].pages[0])
        finally:
            await browser.close()


def test_session_url_opens_a_governed_session(server: dict[str, Any]) -> None:
    url = _session_url(server)

    async def steps(page: Any) -> str:
        await page.goto(server["base"] + "/gov/form")
        await page.fill("#q", "hello")
        return str(await page.title())

    assert asyncio.run(_with_page(url.cdp_url, steps)) == "Form"
    records = actions(server, url.session_id)
    assert records and records[0]["context"]["agent_id"] == AGENT
    assert records[0]["context"]["tenant_id"] == "test"

    # Single use: the same URL does not open a second session (the token is gone: 404).
    with pytest.raises(playwright_api.Error, match="Unexpected status 404"):
        asyncio.run(_with_page(url.cdp_url, steps))


def test_connect_playwright_is_the_short_path(server: dict[str, Any], installed: None) -> None:
    async def steps() -> tuple[str, str, bool]:
        async with playwright_api.async_playwright() as p:
            key = agent_key(AGENT)
            async with await statelock.client.connect_playwright(p, server["base"], api_key=key) as governed:
                await governed.page.goto(server["base"] + "/gov/form")
                await governed.page.fill("#q", "hello")
                title = await governed.page.title()
            return governed.session.agent_id, title, governed.browser.is_connected()

    assert asyncio.run(steps()) == (AGENT, "Form", False)  # leaving the block closed the connection


def test_guard_names_the_rule_with_the_sessions_key(server: dict[str, Any]) -> None:
    """guard() looks the violation up with the key the session was created with (auth is on)."""

    async def steps() -> StatelockPolicyViolationError:
        async with playwright_api.async_playwright() as p:
            key = agent_key("finance_reconciliation_agent")
            async with await statelock.client.connect_playwright(p, server["base"], api_key=key) as governed:
                with pytest.raises(StatelockPolicyViolationError) as raised:
                    async with governed.guard():
                        await governed.page.goto(server["base"] + "/demo/finance?scenario=mismatch")
                        await governed.page.click("text=Mark as Paid")
                        await governed.page.title()
            return raised.value

    violation = asyncio.run(steps())
    assert (violation.rule, violation.agent_id) == ("assert_field_equal", "finance_reconciliation_agent")


def test_session_ws_url_works_directly(server: dict[str, Any]) -> None:
    url = _session_url(server)

    async def session() -> dict[str, Any]:
        async with websockets.connect(url.ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Statelock.session"}))
            return json.loads(await ws.recv())

    assert asyncio.run(session())["result"] == {"session_id": url.session_id, "agent_id": AGENT}


def test_playwright_uploads_and_downloads_work_after_install(
    server: dict[str, Any], installed: None, tmp_path: Path
) -> None:
    url = _session_url(server)

    async def steps(page: Any) -> dict[str, Any]:
        assert await statelock_session(page) == {"session_id": url.session_id, "agent_id": AGENT}
        await page.goto(server["base"] + "/gov/upload")
        # Playwright's own set_input_files, unchanged agent code.
        await page.set_input_files("#file", {"name": "a.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1"})
        picked_page = await page.inner_text("#picked")
        await page.locator("#file").set_input_files(
            [{"name": n, "mimeType": "text/plain", "buffer": b"x"} for n in ("b.txt", "c.txt")]
        )
        picked_locator = await page.inner_text("#picked")

        await page.goto(server["base"] + "/gov/docs")
        # Playwright's own expect_download, unchanged agent code.
        matched: list[str] = []

        def is_report(download: Any) -> bool:
            matched.append(download.suggested_filename)
            return download.suggested_filename == "report.pdf"

        async with page.expect_download(predicate=is_report) as info:
            await page.click("#pdf")
        download = await info.value
        saved = await download.save_as(tmp_path / "report.pdf")
        path = await download.path()
        return {
            "picked": (picked_page, picked_locator),
            "name": download.suggested_filename,
            "saved": Path(saved).read_bytes(),
            "path": Path(path).read_bytes(),
            "failure": await download.failure(),
            "matched": matched,
        }

    result = asyncio.run(_with_page(url.cdp_url, steps))
    assert result["picked"] == ("picked:1", "picked:2")
    assert result["name"] == "report.pdf"
    assert result["saved"] == result["path"] == FILES["report.pdf"]
    assert result["failure"] is None
    assert result["matched"] == ["report.pdf"]
    uploads = [r for r in actions(server, url.session_id) if r["context"]["method"] == "DOM.setFileInputFiles"]
    assert [len(r["context"]["params"]["statelock_uploads"]) for r in uploads] == [1, 2]


def test_sync_playwright_uploads_and_downloads_work_after_install(server: dict[str, Any], tmp_path: Path) -> None:
    statelock.client.install_sync()
    session_id = ""
    try:
        with playwright_sync_api.sync_playwright() as p:
            governed = statelock.client.connect_playwright_sync(p, server["base"], api_key=agent_key(AGENT))
            session_id = governed.session.session_id
            try:
                page = governed.page
                assert statelock.client.statelock_session_sync(page) == {
                    "session_id": governed.session.session_id,
                    "agent_id": AGENT,
                }
                page.goto(server["base"] + "/gov/upload")
                page.set_input_files("#file", {"name": "a.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1"})
                picked_page = page.inner_text("#picked")
                page.locator("#file").set_input_files(
                    [{"name": n, "mimeType": "text/plain", "buffer": b"x"} for n in ("b.txt", "c.txt")]
                )
                picked_locator = page.inner_text("#picked")

                page.goto(server["base"] + "/gov/docs")
                matched: list[str] = []

                def is_report(download: Any) -> bool:
                    matched.append(download.suggested_filename)
                    return download.suggested_filename == "report.pdf"

                with page.expect_download(predicate=is_report) as info:
                    page.click("#pdf")
                download = info.value
                saved = download.save_as(tmp_path / "sync-report.pdf")
                path = download.path()
                assert (picked_page, picked_locator) == ("picked:1", "picked:2")
                assert download.suggested_filename == "report.pdf"
                assert matched == ["report.pdf"]
                assert Path(saved).read_bytes() == Path(path).read_bytes() == FILES["report.pdf"]
            finally:
                governed.close()
    finally:
        statelock.client.uninstall_sync()

    uploads = [r for r in actions(server, session_id) if r["context"]["method"] == "DOM.setFileInputFiles"]
    assert [len(r["context"]["params"]["statelock_uploads"]) for r in uploads] == [1, 2]


def test_blocked_download_raises_through_playwrights_api(server: dict[str, Any], installed: None) -> None:
    url = _session_url(server)

    async def steps(page: Any) -> StatelockPolicyViolationError:
        await page.goto(server["base"] + "/gov/docs")
        with pytest.raises(StatelockPolicyViolationError) as raised:
            async with url.guard(), page.expect_download():
                await page.click("#exe")  # flow_agent allows .pdf and .bin only
        return raised.value

    violation = asyncio.run(_with_page(url.cdp_url, steps))
    assert violation.rule == "restrict_downloads"
    assert violation.recorded and violation.session_id == url.session_id  # the guard added the record


def test_install_leaves_plain_browsers_alone(installed: None) -> None:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")

    async def steps() -> tuple[Any, str]:
        async with playwright_api.async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content("<input id=f type=file><p id=n></p>")
                await page.evaluate("f.onchange = () => { n.textContent = f.files.length }")
                await page.set_input_files("#f", {"name": "a.txt", "mimeType": "text/plain", "buffer": b"a"})
                return await statelock_session(page), str(await page.inner_text("#n"))
            finally:
                await browser.close()

    assert asyncio.run(steps()) == (None, "1")


def test_sync_blocked_download_and_guard(server: dict[str, Any]) -> None:
    """The sync API reports a blocked download, and the sync guard names the rule."""
    statelock.client.install_sync()
    try:
        with playwright_sync_api.sync_playwright() as p:
            with statelock.client.connect_playwright_sync(p, server["base"], api_key=agent_key(AGENT)) as governed:
                page = governed.page
                page.goto(server["base"] + "/gov/docs")
                with (
                    pytest.raises(StatelockPolicyViolationError) as raised,
                    governed.guard(),  # the key the session was created with
                    page.expect_download(),
                ):
                    page.click("#exe")  # flow_agent allows .pdf and .bin only
            assert not governed.browser.is_connected()
    finally:
        statelock.client.uninstall_sync()
    assert raised.value.rule == "restrict_downloads"
    assert "tool.exe" in raised.value.reason


def test_sync_install_leaves_plain_browsers_alone() -> None:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    statelock.client.install_sync()
    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content("<input id=f type=file><p id=n></p>")
                page.evaluate("f.onchange = () => { n.textContent = f.files.length }")
                page.set_input_files("#f", {"name": "a.txt", "mimeType": "text/plain", "buffer": b"a"})
                assert (statelock.client.statelock_session_sync(page), page.inner_text("#n")) == (None, "1")
            finally:
                browser.close()
    finally:
        statelock.client.uninstall_sync()


def test_session_objects_upload_and_download_without_install(server: dict[str, Any], tmp_path: Path) -> None:
    """PlaywrightSession (the session URL path) has the file methods; no install() needed."""

    async def steps() -> tuple[list[dict[str, Any]], str, bytes]:
        async with playwright_api.async_playwright() as p:
            key = agent_key(AGENT)
            governed = await statelock.client.connect_playwright(p, server["base"], api_key=key)
            async with governed, governed.guard():
                page = governed.page
                await page.goto(server["base"] + "/gov/upload")
                stored = await governed.set_input_files("#file", {"name": "a.pdf", "buffer": b"%PDF-1"})
                picked = await page.inner_text("#picked")
                other = await page.context.new_page()
                await other.goto(server["base"] + "/gov/docs")
                async with governed.expect_download(lambda d: d.name == "report.pdf", page=other) as info:
                    await other.click("#pdf")
                download = await info.value
                return stored, picked, (await download.save_as(tmp_path / "r.pdf")).read_bytes()

    stored, picked, data = asyncio.run(steps())
    assert ([f["name"] for f in stored], picked, data) == (["a.pdf"], "picked:1", FILES["report.pdf"])


def test_sync_session_objects_upload_and_download_without_install(server: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    with (
        playwright_sync_api.sync_playwright() as p,
        statelock.client.connect_playwright_sync(p, server["base"], api_key=agent_key(AGENT)) as governed,
        governed.guard(),
    ):
        page = governed.page
        page.goto(server["base"] + "/gov/upload")
        stored = governed.set_input_files(page.locator("#file"), [tmp_path / "b.txt"], timeout=5000)
        assert (stored[0]["mimeType"], page.inner_text("#picked")) == ("text/plain", "picked:1")
        page.goto(server["base"] + "/gov/docs")
        with governed.expect_download(timeout=10_000) as info:
            page.click("#pdf")
        assert info.value.read_bytes() == FILES["report.pdf"]


def test_expect_download_honours_the_default_timeout(server: dict[str, Any]) -> None:
    """Like Playwright's: no timeout given means the page's (or its context's) default timeout."""
    statelock.client.install_sync()
    try:
        with (
            playwright_sync_api.sync_playwright() as p,
            statelock.client.connect_playwright_sync(p, server["base"], api_key=agent_key(AGENT)) as governed,
        ):
            page = governed.page
            page.goto(server["base"] + "/gov/docs")
            page.context.set_default_timeout(1500)
            started = time.monotonic()
            with (
                pytest.raises(statelock.client.StatelockDownloadError, match="no download completed"),
                page.expect_download(predicate=lambda _: False),
            ):
                page.click("#pdf")
            waited = time.monotonic() - started
    finally:
        statelock.client.uninstall_sync()
    assert 1.4 < waited < 15  # the context's 1.5 s, not the 30 s default
