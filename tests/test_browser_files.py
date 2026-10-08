"""Governed uploads and downloads, end to end."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from browser_support import (
    FILES,
    StatelockPolicyViolationError,
    actions,
    download_session,
    network_log,
    run_session,
    upload_session,
    wait_for_proxy_close,
)

pytestmark = pytest.mark.browser


def test_set_input_files_from_page_code_is_blocked(server: dict[str, Any]) -> None:
    # Over connect_over_cdp, Playwright sets files with page code (DataTransfer).
    async def steps(page: Any) -> str:
        await page.set_input_files("#file", files=[{"name": "a.txt", "mimeType": "text/plain", "buffer": b"hi"}])
        await wait_for_proxy_close(page)
        return str(await page.inner_text("#picked"))

    session_id, result = run_session(server, "flow_agent", "/gov/upload", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "untrusted_file_input"
    record = actions(server, session_id, method="Statelock.untrustedFileInput")[-1]
    assert record["context"]["params"]["files"] == ["a.txt"]


def test_files_set_silently_then_real_submit_is_blocked(server: dict[str, Any]) -> None:
    script = """() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['secret'], 'b.txt', {type: 'text/plain'}));
      document.getElementById('file').files = dt.files;
    }"""

    async def steps(page: Any) -> str:
        await page.evaluate(script)
        await page.click("#upgo")
        await wait_for_proxy_close(page)
        return str(await page.title())

    session_id, result = run_session(server, "flow_agent", "/gov/upload", steps)
    assert isinstance(result, StatelockPolicyViolationError)
    assert result.rule == "untrusted_file_input"
    assert not any(e["url"].endswith("/gov/result") for e in network_log(server, session_id))


def test_sdk_set_input_files_is_governed_and_submits(server: dict[str, Any], tmp_path: Path) -> None:
    report = tmp_path / "report.txt"
    report.write_bytes(b"x" * (5 * 1024 * 1024 + 7))  # more than one upload chunk

    async def steps(conn: Any, page: Any) -> Any:
        stored = await conn.set_input_files(page, "#file", [report, {"name": "b.csv", "buffer": b"a,b"}])
        await page.wait_for_function("document.getElementById('picked').textContent === 'picked:2'")
        await page.click("#upgo")
        await page.wait_for_url("**/gov/result")
        return (
            stored,
            await page.title(),
            await page.evaluate("document.querySelectorAll('[data-statelock-upload-target]').length"),
        )

    session_id, result = asyncio.run(upload_session(server, steps))
    assert not isinstance(result, Exception), result
    stored, title, markers = result
    assert title == "Result"
    assert markers == 0
    assert [f["name"] for f in stored] == ["report.txt", "b.csv"]
    assert stored[0]["size"] == report.stat().st_size
    record = next(a for a in actions(server, session_id) if a["context"]["method"] == "DOM.setFileInputFiles")
    assert record["context"]["action_kind"] == "file_upload"
    assert record["verdict"]["decision"] == "allow"
    evidence = record["context"]["params"]["statelock_uploads"]
    assert [e["sha256"] for e in evidence] == [f["sha256"] for f in stored]
    assert not Path(stored[0]["path"]).exists()  # removed with the session


def test_set_file_input_files_with_other_host_paths_is_refused(server: dict[str, Any], tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("proxy host file", encoding="utf-8")

    async def steps(conn: Any, page: Any) -> str:
        cdp = await page.context.new_cdp_session(page)
        document = await cdp.send("DOM.getDocument")
        node = await cdp.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": "#file"})
        await cdp.send("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(secret)]})
        return str(await page.inner_text("#picked"))

    _, result = asyncio.run(upload_session(server, steps))
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "file_upload_path"


def test_download_is_checked_recorded_and_fetched(server: dict[str, Any], tmp_path: Path) -> None:
    async def steps(conn: Any, page: Any) -> Any:
        async with conn.expect_download(page) as info:
            await page.click("#pdf")
        download = await info.value
        saved = await download.save_as(tmp_path / "report.pdf")
        return download, saved.read_bytes()

    session_id, result = asyncio.run(download_session(server, steps))
    assert not isinstance(result, Exception), result
    download, data = result
    assert data == FILES["report.pdf"]
    assert download.name == "report.pdf"
    assert download.sha256 == hashlib.sha256(data).hexdigest()
    records = [r for r in actions(server, session_id) if r["context"]["method"] == "Statelock.download"]
    assert [r["context"]["params"]["phase"] for r in records] == ["begin", "complete"]
    assert all(r["verdict"]["decision"] == "allow" for r in records)
    assert records[1]["context"]["params"]["sha256"] == download.sha256
    assert "/gov/docs" in records[0]["context"]["browser_state"]["url"]


def test_download_with_a_disallowed_type_is_blocked(server: dict[str, Any]) -> None:
    async def steps(conn: Any, page: Any) -> Any:
        async with conn.expect_download(page) as info:
            await page.click("#exe")
        return await info.value

    _, result = asyncio.run(download_session(server, steps))
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "restrict_downloads"
    assert "tool.exe" in result.reason


def test_download_over_the_size_limit_is_blocked(server: dict[str, Any]) -> None:
    async def steps(conn: Any, page: Any) -> Any:
        async with conn.expect_download(page) as info:
            await page.click("#big")
        return await info.value

    _, result = asyncio.run(download_session(server, steps))
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "download_limit"


def test_agent_cannot_redirect_downloads(server: dict[str, Any], tmp_path: Path) -> None:
    elsewhere = tmp_path / "agent-downloads"
    elsewhere.mkdir()

    async def steps(conn: Any, page: Any) -> Any:
        cdp = await conn.browser.new_browser_cdp_session()
        await cdp.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(elsewhere)})
        async with conn.expect_download(page) as info:
            await page.click("#pdf")
        return await info.value

    _, result = asyncio.run(download_session(server, steps))
    assert not isinstance(result, Exception), result
    assert list(elsewhere.iterdir()) == []  # Statelock kept its own folder


def test_download_in_a_new_browser_context_is_governed(server: dict[str, Any]) -> None:
    async def steps(conn: Any, page: Any) -> Any:
        context = await conn.browser.new_context()
        other = await context.new_page()
        await other.goto(server["base"] + "/gov/docs")
        async with conn.expect_download(other) as info:
            await other.click("#pdf")
        download = await info.value
        return await download.read_bytes()

    _, result = asyncio.run(download_session(server, steps))
    assert result == FILES["report.pdf"], result
