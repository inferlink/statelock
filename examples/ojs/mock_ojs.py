# SPDX-License-Identifier: Apache-2.0
"""A small stand-in for an Open Journal Systems (OJS 3.x) editor site, for trying the
OJS agent without a real journal. It copies the parts of the OJS markup the agent
relies on (URLs, the file download links, the decline button, the TinyMCE message
iframe), not OJS itself.

    python examples/ojs/mock_ojs.py            # http://127.0.0.1:8081/index.php/journal
    OJS_BASE_URL=http://127.0.0.1:8081/index.php/journal OJS_USERNAME=editor ...

Log in with editor / mock-editor-pass (Statelock's secrets file holds the password,
not the agent). State (declined papers) lives in memory.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

JOURNAL = "/index.php/journal"
USERNAME = "editor"
PASSWORD = "mock-editor-pass"  # noqa: S105 - a public demo login for the mock site
SESSION_COOKIE = "OJSSID"


def _pdf(title: str, authors: list[str], pages: int = 3) -> bytes:
    """A tiny, valid-looking PDF whose text names the title and authors."""
    body = f"{title} / {', '.join(authors)} " + "text " * 50
    return (f"%PDF-1.4\n% {body}\n" + "%% page\n" * pages + "%%EOF\n").encode()


@dataclass
class SubmissionFile:
    file_id: int
    name: str
    genre: str  # OJS "component": Article Text, Reproducibility Checklist, ...
    content: bytes


@dataclass
class Submission:
    submission_id: int
    title: str
    authors: list[str]
    comments: str
    files: list[SubmissionFile]
    declined: bool = False
    decline_message: str | None = None


@dataclass
class Faults:
    """Ways to make the site misbehave, for tests of the policy's checks."""

    reject_logins: bool = False  # the login fails (bounces back with an error)
    ignore_declines: bool = False  # "Record Editorial Decision" does not record anything
    shown_id_offset: int = 0  # the paper page shows another paper's id than its URL
    broken_tabs: bool = False  # clicking a tab of the submissions list does nothing


@dataclass
class MockJournal:
    submissions: dict[int, Submission] = field(default_factory=dict)
    archived: list[int] = field(default_factory=list)
    faults: Faults = field(default_factory=Faults)

    @classmethod
    def sample(cls) -> MockJournal:
        journal = cls()
        rows = [
            (
                101,
                "Planning with Learned Heuristics",
                ["Ada Lovelace", "Alan Turing"],
                "1. Why it matters: ...\n2. Closest related papers: ...\n3. No.",
                [("manuscript.pdf", "Article Text"), ("checklist.pdf", "Reproducibility Checklist")],
            ),
            (102, "A Note Without Comments", ["Grace Hopper"], "", [("paper.pdf", "Article Text")]),
            (103, "LaTeX Source Submission", ["Edsger Dijkstra"], "See attached.", [("paper.tex", "Article Text")]),
        ]
        file_id = 1
        for submission_id, title, authors, comments, files in rows:
            submission_files = []
            for name, genre in files:
                content = _pdf(title, authors) if name.endswith(".pdf") else b"\\documentclass{article} not a pdf"
                submission_files.append(SubmissionFile(file_id, name, genre, content))
                file_id += 1
            journal.submissions[submission_id] = Submission(submission_id, title, authors, comments, submission_files)
        journal.archived = [90]
        return journal

    def file(self, file_id: int) -> tuple[Submission, SubmissionFile] | None:
        for submission in self.submissions.values():
            for submission_file in submission.files:
                if submission_file.file_id == file_id:
                    return submission, submission_file
        return None


STYLE = """<style>
body{font-family:sans-serif;margin:0} nav{float:left;width:160px;padding:12px;background:#eee;min-height:100vh}
main{margin-left:190px;padding:12px}
.pkpModal{position:fixed;inset:10%;background:#fff;border:2px solid #333;padding:16px}
.panel{display:none} .panel.active{display:block} iframe{width:100%;height:160px;border:1px solid #999}
button,a.pkp_button{margin:4px;padding:6px 10px}
</style>"""


def _page(title: str, content: str, *, nav: bool = True) -> HTMLResponse:
    menu = f'<nav><a href="{JOURNAL}/submissions">Submissions</a></nav>' if nav else ""
    return HTMLResponse(
        f"<!doctype html><html><head><title>{html.escape(title)}</title>{STYLE}</head>"
        f"<body>{menu}<main>{content}</main></body></html>"
    )


async def _form(request: Request) -> dict[str, str]:
    """URL-encoded form fields (no python-multipart needed)."""
    fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}


def _signed_in(request: Request) -> bool:
    return request.cookies.get(SESSION_COOKIE) == "signed-in"


def _to_login() -> RedirectResponse:
    return RedirectResponse(f"{JOURNAL}/login", status_code=302)


def build_router(journal: MockJournal) -> APIRouter:  # noqa: C901 - one route table
    router = APIRouter()

    @router.get(JOURNAL + "/login")
    async def login(error: str = "") -> HTMLResponse:
        message = '<p class="pkp_form_error">Invalid username or password.</p>' if error else ""
        return _page(
            "Login",
            f"""<h1>Login</h1>{message}
<form id="login" method="post" action="{JOURNAL}/login/signIn">
<label for="username">Username</label><input id="username" name="username" autocomplete="username">
<label for="password">Password</label><input id="password" name="password" type="password">
<button class="submit" type="submit">Login</button></form>""",
            nav=False,
        )

    @router.post(JOURNAL + "/login/signIn")
    async def sign_in(request: Request) -> Response:
        form = await _form(request)
        if journal.faults.reject_logins or (form.get("username"), form.get("password")) != (USERNAME, PASSWORD):
            return RedirectResponse(f"{JOURNAL}/login?error=1", status_code=302)
        response = RedirectResponse(f"{JOURNAL}/submissions", status_code=302)
        response.set_cookie(SESSION_COOKIE, "signed-in", httponly=True)
        return response

    @router.get(JOURNAL + "/submissions")
    async def submissions(request: Request) -> Response:
        if not _signed_in(request):
            return _to_login()
        active = "".join(
            f"<li><span>{s.submission_id}</span> {html.escape(s.title)} "
            f'<a href="{JOURNAL}/workflow/access/{s.submission_id}">View</a></li>'
            for s in journal.submissions.values()
            if not s.declined
        )
        archived = "".join(f'<li><a href="{JOURNAL}/workflow/access/{i}">View</a></li>' for i in journal.archived)
        return _page(
            "Submissions",
            f"""<h1>Submissions</h1>
<div role="tablist"><button role="tab" data-panel="queue">My Queue</button>
<button role="tab" data-panel="active">All Active</button>
<button role="tab" data-panel="archive">Archives</button></div>
<section id="queue" class="panel active"><h2>My Queue</h2><p>Nothing assigned.</p></section>
<section id="active" class="panel"><h2>All Active</h2><input placeholder="Search"><ul>{active}</ul></section>
<section id="archive" class="panel"><h2>Archives</h2><ul>{archived}</ul></section>
<script>const broken = {str(journal.faults.broken_tabs).lower()};
document.querySelectorAll('[role=tab]').forEach((tab) => tab.addEventListener('click', () => {{
  if (broken) return;
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === tab.dataset.panel));
}}));</script>""",
        )

    @router.get(JOURNAL + "/workflow/access/{submission_id}")
    async def access(submission_id: int, request: Request) -> Response:
        if not _signed_in(request):
            return _to_login()
        return RedirectResponse(f"{JOURNAL}/workflow/index/{submission_id}/1", status_code=302)

    @router.get(JOURNAL + "/workflow/index/{submission_id}/{stage}")
    async def workflow(submission_id: int, stage: int, request: Request) -> Response:  # noqa: ARG001
        if not _signed_in(request):
            return _to_login()
        submission = journal.submissions.get(submission_id)
        if submission is None:
            return _page("Not found", "<h1>Submission not found</h1>")
        files = "".join(
            f'<li><a class="pkp_linkaction_downloadFile" href="{JOURNAL}/$$$call$$$/api/file/file-api/'
            f'download-file?submissionFileId={f.file_id}&amp;submissionId={submission_id}">{html.escape(f.name)}</a>'
            f" <span>{html.escape(f.genre)}</span></li>"
            for f in submission.files
        )
        notes = (
            f'<div class="note"><h4>Note</h4><div class="noteContent">{html.escape(submission.comments)}</div></div>'
            if submission.comments
            else ""
        )
        authors = "".join(f'<li class="contributor">{html.escape(name)}</li>' for name in submission.authors)
        decisions = (
            "<p><strong>Declined</strong>: This submission is not being considered.</p>"
            if submission.declined
            else f"""<a class="pkp_button" href="#" id="decline-button-{submission_id}x">Decline Submission</a>
<a class="pkp_button" href="#">Send to Review</a><a class="pkp_button" href="#">Accept and Skip Review</a>"""
        )
        return _page(
            f"#{submission_id} {submission.title}",
            f"""<h1><span class="pkpWorkflow__identificationId">{submission_id + journal.faults.shown_id_offset}</span>
{html.escape(submission.title)}</h1>
<div role="tablist"><button role="tab" data-panel="workflow">Workflow</button>
<button role="tab" data-panel="publication">Publication</button></div>
<section id="workflow" class="panel active">
<h2>Submission Files</h2><ul>{files}</ul>
<a href="#" id="editor-comments">Comments for the Editor</a>
<div id="decisions">{decisions}</div></section>
<section id="publication" class="panel"><button role="tab" data-panel="contributors">Contributors</button>
<div id="contributors" class="panel"><h3>List of Contributors</h3><ul>{authors}</ul></div></section>
<div id="comments-modal" class="pkpModal" hidden><h2>Comments for the Editor</h2><h3>Messages</h3>{notes}
<button class="pkpModal__close" aria-label="Close">x</button></div>
<div id="decline-modal" class="pkpModal" hidden><h2>Decline Submission</h2>
<form id="decline-form" method="post" action="{JOURNAL}/workflow/decline/{submission_id}">
<input type="hidden" name="submissionId" value="{submission_id}">
<input type="hidden" name="personalMessage" id="personalMessage-value">
<label>Send an email to the author(s)</label>
<iframe id="personalMessage-{submission_id}_ifr" srcdoc="<html><body contenteditable='true'></body></html>"></iframe>
<p>Select review files to share with the author(s)</p>
<button type="submit" class="submitFormButton">Record Editorial Decision</button>
<button type="button" class="cancelButton">Cancel</button></form></div>
<script>
const show = (id, on) => {{ document.getElementById(id).hidden = !on; }};
document.querySelectorAll('[role=tab]').forEach((tab) => tab.addEventListener('click', () => {{
  const target = document.getElementById(tab.dataset.panel);
  target.parentElement.querySelectorAll(':scope > .panel').forEach((p) => p.classList.toggle('active', p === target));
  target.classList.add('active');
}}));
document.getElementById('editor-comments').addEventListener('click', (e) => {{
  e.preventDefault(); show('comments-modal', true); }});
document.querySelector('#comments-modal .pkpModal__close')
  .addEventListener('click', () => show('comments-modal', false));
document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') show('comments-modal', false); }});
const decline = document.querySelector("a[id^='decline-button-']");
if (decline) decline.addEventListener('click', (e) => {{ e.preventDefault(); show('decline-modal', true); }});
document.querySelector('#decline-modal .cancelButton').addEventListener('click', () => show('decline-modal', false));
document.getElementById('decline-form').addEventListener('submit', () => {{
  const body = document.querySelector('#decline-modal iframe').contentDocument.body;
  document.getElementById('personalMessage-value').value = body.innerText;
}});
</script>""",
        )

    @router.post(JOURNAL + "/workflow/decline/{submission_id}")
    async def record_decline(submission_id: int, request: Request) -> Response:
        if not _signed_in(request):
            return _to_login()
        submission = journal.submissions.get(submission_id)
        if submission is not None and not journal.faults.ignore_declines:
            submission.declined = True
            submission.decline_message = (await _form(request)).get("personalMessage", "")
        return RedirectResponse(f"{JOURNAL}/workflow/index/{submission_id}/1", status_code=303)

    @router.get(JOURNAL + "/$$$call$$$/api/file/file-api/download-file")
    async def download(submissionFileId: int, request: Request) -> Response:
        if not _signed_in(request):
            return _to_login()
        found = journal.file(submissionFileId)
        if found is None:
            return Response(status_code=404)
        _, submission_file = found
        media = "application/pdf" if submission_file.name.endswith(".pdf") else "application/octet-stream"
        return Response(
            submission_file.content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{submission_file.name}"'},
        )

    return router


def create_app(journal: MockJournal | None = None) -> FastAPI:
    app = FastAPI(title="Mock OJS")
    app.state.journal = journal or MockJournal.sample()
    app.include_router(build_router(app.state.journal))
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_app(), host=os.getenv("MOCK_OJS_HOST", "127.0.0.1"), port=int(os.getenv("MOCK_OJS_PORT", "8081"))
    )
