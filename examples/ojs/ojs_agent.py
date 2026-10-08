# SPDX-License-Identifier: Apache-2.0
"""An OJS (Open Journal Systems) desk-screening agent, governed by Statelock.

The agent logs in as an editor (unless its saved browser session is still logged
in), lists the active submissions, and for each one
downloads the manuscript, reads the comments to the editor and the contributors,
and runs basic checks. Papers listed with ``"reject": true`` in the decisions file
are then declined with the email given there. Recording a decision sends that
email to the authors, so the default is a dry run: the decline form is filled and
cancelled. Set OJS_RECORD_DECISIONS=1 to record.

Stagehand finds elements with an LLM when the fixed OJS selectors miss (set a
model key to enable it). The agent is ordinary Stagehand and Playwright code; the
Statelock part is three lines in open_session() and run_governed() (marked
"Statelock"). Every click, key
press and download then goes through Statelock.

The agent never has the password. It types the placeholder {{secret:ojs_password}}
into the password field, and Statelock types the real password there (from its
secrets file, only for this agent, on the login page). Statelock also keeps the
browser's login between runs (a saved session), so later runs skip the login.

Environment:
  STATELOCK_URL           http://localhost:8010 (the Statelock server)
  STATELOCK_API_KEY       the agent's Statelock key
  OJS_BASE_URL            http://127.0.0.1:8081/index.php/journal (the mock site: mock_ojs.py)
  OJS_USERNAME            the editor's user name
  OJS_PASSWORD_SECRET     the secret Statelock types as the password (default ojs_password)
  OJS_SAVED_SESSION       the saved browser session (default ojs-editor; empty: log in every run)
  OJS_ALLOW_REMOTE        1 to allow a host other than localhost (a real journal)
  OJS_DECISIONS_FILE      JSON: {"<paper id>": {"reject": true, "email": "..."}} (optional)
  OJS_RECORD_DECISIONS    1 to record declines (sends the email); default: dry run
  OJS_OUTPUT_DIR          where manuscripts and results.json go (default ./ojs-output)
  OJS_ACTION_TIMEOUT      seconds an action may take, including a wait for review (default 360)
  OJS_DEMO_PROHIBITED_CLICK  demo only: after logging in, click this workflow button on the
                          first paper (for example "Send to Review") to show Statelock blocking it
  STAGEHAND_MODEL, MODEL_API_KEY   optional: the LLM for Stagehand's observe()
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from playwright.async_api import Locator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from stagehand import ObserveResult, Stagehand, StagehandConfig

import statelock.client
from statelock.client import SessionUrl, StatelockPolicyViolationError, create_session_url

logger = logging.getLogger("ojs_agent")
T = TypeVar("T")

PAPER_ID_RE = re.compile(r"/(?:workflow/(?:access|index)|authorDashboard/submission)/(\d+)")
# The mock journal: on this machine, or the compose demo's ojs-mock service.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ojs-mock"}
# Actions paused for review (policy rules with on_fail: review) wait for a person.
DEFAULT_ACTION_TIMEOUT_S = 360.0


def xpath_literal(text: str) -> str:
    """An XPath string literal for any text (a quote in it would end a '...' literal)."""
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    return "concat('" + "', \"'\", '".join(text.split("'")) + "')"


@dataclass
class AgentSettings:
    base_url: str
    username: str
    password_secret: str
    saved_session: str | None
    decisions_file: Path | None
    record_decisions: bool
    output_dir: Path
    model_name: str | None
    model_api_key: str | None = field(repr=False)
    demo_prohibited_click: str | None = None
    action_timeout_s: float = DEFAULT_ACTION_TIMEOUT_S

    @classmethod
    def from_env(cls) -> AgentSettings:
        decisions = os.getenv("OJS_DECISIONS_FILE")
        settings = cls(
            base_url=os.getenv("OJS_BASE_URL", "http://127.0.0.1:8081/index.php/journal").rstrip("/"),
            username=os.environ["OJS_USERNAME"],
            password_secret=os.getenv("OJS_PASSWORD_SECRET", "ojs_password"),
            saved_session=os.getenv("OJS_SAVED_SESSION", "ojs-editor") or None,
            decisions_file=Path(decisions) if decisions else None,
            record_decisions=os.getenv("OJS_RECORD_DECISIONS") == "1",
            output_dir=Path(os.getenv("OJS_OUTPUT_DIR", "ojs-output")),
            model_name=os.getenv("STAGEHAND_MODEL"),
            model_api_key=os.getenv("MODEL_API_KEY"),
            demo_prohibited_click=os.getenv("OJS_DEMO_PROHIBITED_CLICK") or None,
            action_timeout_s=float(os.getenv("OJS_ACTION_TIMEOUT") or DEFAULT_ACTION_TIMEOUT_S),
        )
        host = urlparse(settings.base_url).hostname or ""
        if host not in LOCAL_HOSTS and os.getenv("OJS_ALLOW_REMOTE") != "1":
            raise SystemExit(f"OJS_BASE_URL points at {host}; set OJS_ALLOW_REMOTE=1 to use a real journal")
        return settings

    @property
    def ai_enabled(self) -> bool:
        return bool(self.model_name and self.model_api_key)


@dataclass
class Screening:
    paper_id: int
    url: str = ""
    files: list[dict[str, str]] = field(default_factory=list)
    manuscript: dict[str, Any] | None = None
    comments: str = ""
    authors: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    decline: str | None = None  # dry_run | recorded | None


def basic_checks(screening: Screening) -> dict[str, bool]:
    """Checks that need no model. Plug journal-specific checks in here (for
    example, an LLM comparing the PDF with the journal's template)."""
    manuscript = screening.manuscript or {}
    return {
        "has_manuscript_pdf": bool(manuscript.get("is_pdf")),
        "has_comments_to_editor": bool(screening.comments.strip()),
        "has_contributors": bool(screening.authors),
    }


class OjsAgent:
    def __init__(self, settings: AgentSettings, stagehand: Stagehand) -> None:
        self.settings = settings
        self.stagehand = stagehand

    @property
    def page(self) -> Any:
        """Stagehand's page (a Playwright page with observe/act/extract)."""
        return self.stagehand.page

    def url(self, path: str) -> str:
        return f"{self.settings.base_url}/{path.lstrip('/')}"

    # Finding elements -----------------------------------------------------------

    async def find(self, selector: str, instruction: str) -> Locator:
        """A fixed OJS selector first; Stagehand's observe() (LLM) when it misses."""
        locator = self.page.locator(selector).first
        if await locator.count() and await locator.is_visible():
            return locator
        if self.settings.ai_enabled:
            results = await self.page.observe(instruction)
            if results:
                logger.info("observe() found %r for: %s", results[0].selector, instruction)
                return self.page.locator(results[0].selector).first
        raise LookupError(f"Could not find: {instruction} ({selector})")

    # Steps ----------------------------------------------------------------------

    async def login(self) -> None:
        await self.page.goto(self.url("submissions"))
        await self.page.wait_for_load_state("networkidle")
        if "/login" not in self.page.url:
            logger.info("Already logged in (saved session)")
            return
        # A placeholder, not the password: Statelock types the secret into the field.
        password = "{{secret:" + self.settings.password_secret + "}}"
        await (await self.find("#username", "the Username text box")).fill(self.settings.username)
        await (await self.find("input[type=password]", "the Password text box")).fill(password)
        await (await self.find("form#login button[type=submit]", "the Login button")).click()
        await self.page.wait_for_load_state("networkidle")
        if "/login" in self.page.url:
            raise RuntimeError("Login failed: still on the login page")
        logger.info("Logged in as %s", self.settings.username)

    async def active_submission_ids(self) -> list[int]:
        await self.page.goto(self.url("submissions"))
        await (await self.find("[role=tab]:has-text('All Active')", "the All Active tab")).click()
        # Only visible links: OJS keeps the other tabs' lists in the DOM.
        hrefs = await self.page.eval_on_selector_all(
            'a[href*="/workflow/"], a[href*="/authorDashboard/submission/"]',
            "(els) => els.filter((el) => el.offsetParent !== null).map((el) => el.getAttribute('href') || '')",
        )
        ids: list[int] = []
        for href in hrefs:
            match = PAPER_ID_RE.search(href)
            if match and int(match.group(1)) not in ids:
                ids.append(int(match.group(1)))
        logger.info("Active submissions: %s", ids)
        return ids

    async def demo_prohibited_click(self, text: str) -> None:
        """Demo: the kind of step a misled model might take. The policy blocks it and ends the session."""
        paper_id = (await self.active_submission_ids())[0]
        await self.open_submission(paper_id)
        logger.warning("Demo: clicking %r on paper %s, a button the policy prohibits", text, paper_id)
        selector = f"xpath=//a[normalize-space()={xpath_literal(text)}]"
        button = ObserveResult(selector=selector, description=text, method="click", arguments=[])
        browser = self.page.context.browser
        closed = asyncio.Event()
        if browser is not None:
            browser.once("disconnected", lambda _browser: closed.set())
        await self.page.act(button)  # Stagehand's click (el.click()), replayed and checked by Statelock
        # Statelock ends the session: wait for that, not a fixed delay.
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(closed.wait(), timeout=10)
        await self.page.title()  # the next command sees that the session ended

    async def open_submission(self, paper_id: int) -> None:
        await self.page.goto(self.url(f"workflow/access/{paper_id}"), wait_until="networkidle")
        match = PAPER_ID_RE.search(self.page.url)
        if not match or int(match.group(1)) != paper_id:
            raise RuntimeError(f"Opening paper {paper_id} landed on {self.page.url}")

    async def submission_files(self) -> list[tuple[Locator, dict[str, str]]]:
        rows = self.page.locator("a.pkp_linkaction_downloadFile")
        found = []
        for index in range(await rows.count()):
            link = rows.nth(index)
            row_text = await link.evaluate("(el) => (el.closest('li, tr') || el).textContent || ''")
            found.append((link, {"name": (await link.inner_text()).strip(), "row": " ".join(row_text.split())}))
        return found

    async def download_manuscript(self, screening: Screening) -> None:
        files = await self.submission_files()
        screening.files = [info for _, info in files]
        article = [(link, info) for link, info in files if "Article" in info["row"]]
        if not article:
            logger.warning("Paper %s: no Article Text file", screening.paper_id)
            return
        link = article[0][0]
        async with self.page.expect_download() as info:
            await link.click()
        download = await info.value
        # The name comes from the site: only its last part, so the file stays in output_dir.
        name = Path(download.suggested_filename).name or "manuscript"
        target = self.settings.output_dir / f"paper-{screening.paper_id}-{name}"
        await download.save_as(target)
        data = target.read_bytes()
        screening.manuscript = {
            "name": download.suggested_filename,
            "path": str(target),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            # The journal wants a PDF: a .tex or .docx manuscript fails this check.
            "is_pdf": data[:5] == b"%PDF-",
        }

    async def read_comments(self, screening: Screening) -> None:
        await (
            await self.find(
                "#editor-comments, a:has-text('Comments for the Editor')", "the Comments for the Editor link"
            )
        ).click()
        notes = self.page.locator(".pkpModal:visible .noteContent")
        screening.comments = "\n\n".join(t.strip() for t in await notes.all_inner_texts())
        close = self.page.locator(".pkpModal:visible .pkpModal__close, .pkpModal:visible [aria-label=Close]")
        if await close.count():
            await close.first.click()
        else:
            await self.page.keyboard.press("Escape")
        await self.page.locator(".pkpModal:visible").wait_for(state="hidden", timeout=5000)

    async def read_contributors(self, screening: Screening) -> None:
        await (await self.find("[role=tab]:has-text('Publication')", "the Publication tab")).click()
        await (await self.find("[role=tab]:has-text('Contributors')", "the Contributors tab")).click()
        names = self.page.locator(".contributor:visible")
        await names.first.wait_for(timeout=5000)
        screening.authors = [n.strip() for n in await names.all_inner_texts() if n.strip()]

    async def screen(self, paper_id: int) -> Screening:
        screening = Screening(paper_id)
        await self.open_submission(paper_id)
        screening.url = self.page.url
        await self.download_manuscript(screening)
        await self.read_comments(screening)
        await self.read_contributors(screening)
        screening.checks = basic_checks(screening)
        logger.info("Paper %s checks: %s", paper_id, screening.checks)
        return screening

    async def decline(self, screening: Screening, email: str) -> None:
        paper_id = screening.paper_id
        # Only a basic check: Statelock checks the length and that it is the decided email.
        if not email.strip():
            raise ValueError(f"Paper {paper_id}: no decline email")
        await self.open_submission(paper_id)
        await (await self.find("a[id^='decline-button-']", "the Decline Submission button")).click()
        editor = self.page.frame_locator("iframe[id*='personalMessage'], iframe[id$='_ifr']").first.locator("body")
        await editor.click()
        # Typed into the editor, so the email sent to the authors is in the evidence.
        paragraphs = [f"Regarding submission {paper_id}:", *email.strip().split("\n\n")]
        for index, paragraph in enumerate(paragraphs):
            if index:
                await self.page.keyboard.press("Enter")
            await self.page.keyboard.insert_text(paragraph)
        if not self.settings.record_decisions:
            await (await self.find(".pkpModal:visible .cancelButton", "the Cancel button")).click()
            screening.decline = "dry_run"
            logger.info("Paper %s: decline filled and cancelled (dry run)", paper_id)
            return
        await (
            await self.find("button:has-text('Record Editorial Decision')", "the Record Editorial Decision button")
        ).click()
        await self.page.wait_for_load_state("networkidle")
        if await self.page.locator("a[id^='decline-button-']").count():
            raise RuntimeError(f"Paper {paper_id}: the decline was not recorded")
        screening.decline = "recorded"
        logger.info("Paper %s: declined", paper_id)

    def decisions(self) -> dict[int, dict[str, Any]]:
        if self.settings.decisions_file is None:
            return {}
        raw = json.loads(self.settings.decisions_file.read_text(encoding="utf-8"))
        return {int(paper_id): decision for paper_id, decision in raw.items()}

    async def run(self) -> list[Screening]:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        await self.login()
        if self.settings.demo_prohibited_click:
            await self.demo_prohibited_click(self.settings.demo_prohibited_click)
        screenings = []
        for paper_id in await self.active_submission_ids():
            try:
                screenings.append(await self.screen(paper_id))
            except (LookupError, RuntimeError, TimeoutError, PlaywrightTimeoutError) as error:
                # One paper's page problem skips that paper. A Statelock violation is
                # not caught here: it ends the session and the run.
                logger.warning("Paper %s skipped: %s", paper_id, error)
        decisions = self.decisions()
        for screening in screenings:
            decision = decisions.get(screening.paper_id) or {}
            if decision.get("reject") is True:
                await self.decline(screening, str(decision.get("email") or ""))
        results = self.settings.output_dir / "results.json"
        results.write_text(json.dumps([asdict(s) for s in screenings], indent=2), encoding="utf-8")
        logger.info("Wrote %s", results)
        return screenings


async def start_stagehand(settings: AgentSettings, browser_url: str) -> Stagehand:
    """Ordinary Stagehand, on the browser at browser_url."""
    config: dict[str, Any] = {"verbose": 0}
    if settings.ai_enabled:
        config.update(model_name=settings.model_name, model_api_key=settings.model_api_key)
    stagehand = Stagehand(StagehandConfig(env="LOCAL", local_browser_launch_options={"cdp_url": browser_url}, **config))
    await stagehand.init()
    return stagehand


def open_session(settings: AgentSettings, server_url: str | None = None, api_key: str | None = None) -> SessionUrl:
    """Statelock: a one-time browser URL (STATELOCK_URL, STATELOCK_API_KEY), with the saved login (blocking)."""
    return create_session_url(
        server_url, api_key=api_key, saved_session=settings.saved_session, save_session=bool(settings.saved_session)
    )


async def run_governed(settings: AgentSettings, session: SessionUrl, steps: Callable[[OjsAgent], Awaitable[T]]) -> T:
    """Run ``steps`` (OjsAgent.run for the full agent) with ordinary Stagehand on the
    session's browser. A Statelock violation raises StatelockPolicyViolationError."""
    statelock.client.install()  # Statelock: Playwright's own uploads and downloads go through Statelock
    stagehand = await start_stagehand(settings, session.cdp_url)
    stagehand.page.context.set_default_timeout(settings.action_timeout_s * 1000)
    try:
        async with session.guard():  # Statelock (optional): a violation raises StatelockPolicyViolationError
            return await steps(OjsAgent(settings, stagehand))
    finally:
        await stagehand.close()


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = AgentSettings.from_env()
    session = await asyncio.to_thread(open_session, settings)  # a blocking HTTP call: off the event loop
    try:
        await run_governed(settings, session, OjsAgent.run)
    except StatelockPolicyViolationError as violation:
        logger.error("Statelock stopped the agent: %s: %s", violation.rule, violation.reason)  # noqa: TRY400
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
