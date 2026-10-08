"""Actions and page text inside iframes and shadow roots, and keys named only by their text."""

from __future__ import annotations

import http.server
import socketserver
import threading
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")

from browser_support import (
    StatelockPolicyViolationError,
    actions,
    run_session,
    running_server,
    wait_for_proxy_close,
)
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

pytestmark = pytest.mark.browser

POLICY = """
policies:
  - agent_id: click_agent
    target_url_contains: /fr/
    pre_conditions:
      - prohibit_click_text: {values: [Delete]}
  - agent_id: text_agent
    target_url_contains: /fr/
    pre_conditions:
      - prohibit_page_text: {values: [Forbidden phrase]}
  - agent_id: enter_agent
    target_url_contains: /fr/
    post_conditions:
      - trigger: {key: [Enter]}
        require_page_text: {values: [Text that never appears]}
"""
AGENTS = ("click_agent", "text_agent", "enter_agent")

BUTTON = "style='display:block;width:200px;height:40px;margin:0'"
TARGETS = f"""<html><head><title>Targets</title></head><body style="margin:0">
<button id=fine {BUTTON} onclick="document.title='fine'">Fine</button>
<iframe id=same srcdoc="<body style='margin:0'><button id=del {BUTTON}
  onclick='parent.document.title=&quot;deleted&quot;'>Delete</button></body>"
  style="display:block;width:300px;height:60px;border:0"></iframe>
<div id=open style="width:200px;height:40px"></div>
<div id=closed style="width:200px;height:40px"></div>
<iframe id=remote src="__REMOTE__" style="display:block;width:300px;height:60px;border:0"></iframe>
<script>
  document.getElementById('open').attachShadow({{mode: 'open'}}).innerHTML = "<button {BUTTON}>Delete</button>";
  const closed = document.getElementById('closed').attachShadow({{mode: 'closed'}});
  closed.innerHTML = "<button {BUTTON}>Delete</button>";
  window.closedButton = closed.querySelector('button');
</script></body></html>"""
REMOTE = f"<html><body style='margin:0'><button {BUTTON}>Delete</button><p>Forbidden phrase</p></body></html>"
TEXT = {
    "open": "<div id=h></div><script>document.getElementById('h').attachShadow({mode:'open'})"
    ".innerHTML='<p>Forbidden phrase</p>'</script>",
    "frame": "<iframe srcdoc='<p>Forbidden phrase</p>'></iframe>",
    "nested": "<div id=h></div><script>document.getElementById('h').attachShadow({mode:'open'})"
    ".innerHTML=\"<iframe srcdoc='<p>Forbidden phrase</p>'></iframe>\"</script>",
    "remote": "<iframe src='__REMOTE__'></iframe>",
    "hidden": "<iframe style='display:none' srcdoc='<p>Forbidden phrase</p>'></iframe>"
    "<div id=h hidden></div><script>document.getElementById('h').attachShadow({mode:'open'})"
    ".innerHTML='<p>Forbidden phrase</p>'</script>",
}
FORM = """<html><head><title>Form</title></head><body>
<form id=f action="/fr/done" method=post><input id=q name=q><button id=go>Go</button></form></body></html>"""


class _RemoteSite(http.server.BaseHTTPRequestHandler):
    """Another origin (127.0.0.2): its frames are cross-origin to the test site."""

    def do_GET(self) -> None:
        body = REMOTE.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


REMOTE_URL: list[str] = []  # set by the fixture


def frames_router() -> APIRouter:
    router = APIRouter()

    async def targets() -> HTMLResponse:
        return HTMLResponse(TARGETS.replace("__REMOTE__", REMOTE_URL[0]))

    async def text(where: str) -> HTMLResponse:
        body = TEXT[where].replace("__REMOTE__", REMOTE_URL[0])
        return HTMLResponse(f"<html><head><title>Text</title></head><body><p>Allowed</p>{body}</body></html>")

    async def form() -> HTMLResponse:
        return HTMLResponse(FORM)

    async def done() -> HTMLResponse:
        return HTMLResponse("<html><head><title>Done</title></head><body>Done</body></html>")

    router.add_api_route("/fr/targets", targets, methods=["GET"])
    router.add_api_route("/fr/text/{where}", text, methods=["GET"])
    router.add_api_route("/fr/form", form, methods=["GET"])
    router.add_api_route("/fr/done", done, methods=["GET", "POST"])
    return router


@pytest.fixture(scope="module")
def frames(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    with socketserver.ThreadingTCPServer(("127.0.0.2", 0), _RemoteSite) as remote:
        remote.daemon_threads = True
        remote.block_on_close = False
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        REMOTE_URL[:] = [f"http://127.0.0.2:{remote.server_address[1]}/elsewhere"]
        try:
            with running_server(tmp_path_factory, policy=POLICY, agents=AGENTS, routers=(frames_router(),)) as running:
                yield running
        finally:
            remote.shutdown()


# Where each button is, in the page's viewport: (x, y) of a point inside it.
POINTS = {
    "fine": "(() => { const r = document.getElementById('fine').getBoundingClientRect(); return [r.x + 20, r.y + 20] })()",
    "srcdoc_frame": """(() => {
        const frame = document.getElementById('same').getBoundingClientRect();
        const button = document.getElementById('same').contentDocument.getElementById('del').getBoundingClientRect();
        return [frame.x + button.x + 20, frame.y + button.y + 20] })()""",
    "open_shadow": "(() => { const r = document.getElementById('open').getBoundingClientRect(); return [r.x + 20, r.y + 20] })()",
    "closed_shadow": "(() => { const r = closedButton.getBoundingClientRect(); return [r.x + 20, r.y + 20] })()",
    "remote_frame": "(() => { const r = document.getElementById('remote').getBoundingClientRect(); return [r.x + 20, r.y + 20] })()",
}


@pytest.mark.parametrize("where", ["srcdoc_frame", "open_shadow", "closed_shadow", "remote_frame"])
def test_prohibited_button_inside_frames_and_shadow_roots_is_blocked(frames: dict[str, Any], where: str) -> None:
    async def steps(page: Any) -> str:
        x, y = await page.evaluate(POINTS["fine"])
        await page.mouse.click(x, y)  # an ordinary button still works
        assert await page.title() == "fine"
        x, y = await page.evaluate(POINTS[where])
        await page.mouse.click(x, y)
        return str(await page.title())

    session_id, result = run_session(frames, "click_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
    target = actions(frames, session_id)[-1]["context"]["browser_state"]["target_element"]
    if where == "remote_frame":
        assert target["unresolved"] is True and target["tag_name"] == "IFRAME"
    else:
        assert target["text"] == "Delete" and target["tag_name"] == "BUTTON"


FOCUS = {
    "srcdoc_frame": "document.getElementById('same').contentDocument.getElementById('del').focus()",
    "open_shadow": "document.getElementById('open').shadowRoot.querySelector('button').focus()",
    "closed_shadow": "closedButton.focus()",
    "remote_frame": "document.getElementById('remote').focus()",
}


@pytest.mark.parametrize("where", list(FOCUS))
def test_enter_on_a_focused_button_inside_frames_and_shadow_roots_is_blocked(
    frames: dict[str, Any], where: str
) -> None:
    async def steps(page: Any) -> str:
        await page.evaluate(FOCUS[where])
        await page.keyboard.press("Enter")
        return str(await page.title())

    session_id, result = run_session(frames, "click_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
    target = actions(frames, session_id)[-1]["context"]["browser_state"]["target_element"]
    assert target["source"] == "focus"
    assert target["unresolved"] if where == "remote_frame" else target["text"] == "Delete"


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "keyDown", "text": "\r"}, {"type": "keyUp"}],
        [{"type": "keyDown", "unmodifiedText": "\r"}],
        [{"type": "rawKeyDown"}, {"type": "char", "text": "\r"}],
        [{"type": "char", "unmodifiedText": "\r"}],
        [{"type": "char", "text": " "}],
    ],
    ids=["keydown_text", "keydown_unmodified_text", "raw_then_char", "char_unmodified_text", "char_space"],
)
def test_enter_or_space_named_only_by_text_is_governed(frames: dict[str, Any], events: list[dict[str, Any]]) -> None:
    async def steps(page: Any) -> str:
        await page.evaluate(FOCUS["srcdoc_frame"])
        cdp = await page.context.new_cdp_session(page)
        for params in events:
            await cdp.send("Input.dispatchKeyEvent", params)
        return str(await page.title())

    _, result = run_session(frames, "click_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"


def test_enter_trigger_fires_for_enter_named_only_by_text(frames: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.focus("#fine")
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r"})
        return str(await page.title())

    session_id, result = run_session(frames, "enter_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "require_page_text"
    assert actions(frames, session_id)[-1]["post_verdict"]["decision"] == "block"


@pytest.mark.parametrize("where", ["open", "frame", "nested"])
def test_page_text_includes_shadow_roots_and_same_origin_frames(frames: dict[str, Any], where: str) -> None:
    async def steps(page: Any) -> str:
        await page.mouse.move(5, 5)
        return str(await page.title())

    session_id, result = run_session(frames, "text_agent", f"/fr/text/{where}", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_page_text"
    state = actions(frames, session_id)[-1]["context"]["browser_state"]
    assert "Forbidden phrase" in state["page_text"] and state["page_text_unread"] == []


def test_hidden_frames_and_shadow_roots_are_not_page_text(frames: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.mouse.move(5, 5)
        return str(await page.title())

    session_id, result = run_session(frames, "text_agent", "/fr/text/hidden", steps)
    assert result == "Text"
    assert actions(frames, session_id)[-1]["context"]["browser_state"]["page_text"] == "Allowed"


def test_text_of_a_cross_origin_frame_is_reported_unread(frames: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.mouse.move(5, 5)
        return str(await page.title())

    session_id, _ = run_session(frames, "text_agent", "/fr/text/remote", steps)
    state = actions(frames, session_id)[-1]["context"]["browser_state"]
    assert "Forbidden phrase" not in state["page_text"]
    assert state["page_text_unread"] == REMOTE_URL


def test_synthetic_click_in_a_srcdoc_frame_is_cancelled(frames: dict[str, Any]) -> None:
    # The frame's own URL (about:srcdoc) matches no policy; it is governed like its page.
    script = """() => {
        const button = document.getElementById('same').contentDocument.getElementById('del');
        return button.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }"""

    async def steps(page: Any) -> str:
        delivered = await page.evaluate(script)
        assert delivered is False  # cancelled before the button's handler ran
        await wait_for_proxy_close(page)
        return str(await page.title())

    session_id, result = run_session(frames, "click_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "synthetic_event"
    record = actions(frames, session_id, method="Statelock.syntheticEvent")[-1]
    assert record["context"]["browser_state"]["title"] == "Targets"


def test_synthetic_click_in_a_cross_site_frame_is_cancelled(frames: dict[str, Any]) -> None:
    # The frame runs in its own process, on an origin no policy names: it is governed
    # because the page it is in is.
    async def steps(page: Any) -> str:
        remote = next(frame for frame in page.frames if frame.url == REMOTE_URL[0])
        delivered = await remote.evaluate(
            "document.querySelector('button').dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))"
        )
        assert delivered is False
        await wait_for_proxy_close(page)
        return str(await page.title())

    _, result = run_session(frames, "click_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "synthetic_event"


def test_agent_code_request_in_a_cross_site_frame_is_blocked(frames: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        remote = next(frame for frame in page.frames if frame.url == REMOTE_URL[0])
        await remote.evaluate("fetch('/elsewhere', {method: 'POST'}).catch(() => {})")
        await wait_for_proxy_close(page)
        return str(await page.title())

    _, result = run_session(frames, "click_agent", "/fr/targets", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "agent_code_request"


def test_request_submit_from_agent_code_is_an_untrusted_submission(frames: dict[str, Any]) -> None:
    # form.requestSubmit() fires a trusted submit event, but no user gesture made it.
    async def steps(page: Any) -> str:
        await page.evaluate("setTimeout(() => document.getElementById('f').requestSubmit(), 0)")
        await wait_for_proxy_close(page)
        return str(await page.title())

    _, result = run_session(frames, "click_agent", "/fr/form", steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "untrusted_form_submission"


def test_real_click_and_enter_still_submit_the_form(frames: dict[str, Any]) -> None:
    async def steps(page: Any) -> str:
        await page.click("#go")
        await page.wait_for_url("**/fr/done")
        await page.goto(page.url.replace("/fr/done", "/fr/form"))
        await page.fill("#q", "x")
        await page.press("#q", "Enter")
        await page.wait_for_url("**/fr/done")
        return str(await page.title())

    _, result = run_session(frames, "click_agent", "/fr/form", steps)
    assert result == "Done"
