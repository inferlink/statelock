"""The OJS example (examples/ojs) end to end: Stagehand through Statelock, on the mock OJS site.

Skipped without Stagehand (pip install "stagehand>=0.4,<0.5") or Chromium.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
pytest.importorskip("stagehand")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ojs"
if not EXAMPLE.is_dir():
    pytest.skip("examples/ojs is not available", allow_module_level=True)
sys.path.insert(0, str(EXAMPLE))

import mock_ojs  # noqa: E402
import ojs_agent  # noqa: E402
from browser_support import actions, agent_key, chromium_available, running_server  # noqa: E402
from ojs_support import RULE_MODULE, ojs_policy, write_decisions  # noqa: E402
from stagehand import ObserveResult  # noqa: E402

import statelock.client  # noqa: E402
from statelock.client import StatelockPolicyViolationError, create_session_url  # noqa: E402

pytestmark = pytest.mark.browser

AGENT = "ojs_screening_agent"
EMAIL = "Thank you for your submission. It lacks the comments to the editor we require."


@pytest.fixture(scope="module")
def ojs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    journal = mock_ojs.MockJournal.sample()
    # The editors' decisions as Statelock's ojs_decision rule reads them (tests change them).
    statelock_decisions = tmp_path_factory.mktemp("decisions") / "decisions.json"
    write_decisions(statelock_decisions, {102: {"reject": True, "email": EMAIL}})
    routers = (mock_ojs.build_router(journal),)
    # The password is in Statelock's secrets file, never in the agent.
    secrets = (("ojs_password", [AGENT], mock_ojs.JOURNAL + "/login", mock_ojs.PASSWORD),)
    with running_server(
        tmp_path_factory,
        policy=ojs_policy(statelock_decisions),
        agents=(AGENT,),
        routers=routers,
        secrets=secrets,
        rule_modules=RULE_MODULE,
    ) as server:
        server["journal"] = journal
        server["decisions"] = statelock_decisions
        yield server


def _settings(server: dict[str, Any], tmp_path: Path, **changes: Any) -> ojs_agent.AgentSettings:
    settings = ojs_agent.AgentSettings(
        base_url=server["base"] + mock_ojs.JOURNAL,
        username=mock_ojs.USERNAME,
        password_secret="ojs_password",  # noqa: S106 - the secret's name, not a password
        saved_session=None,
        decisions_file=None,
        record_decisions=False,
        output_dir=tmp_path / "out",
        model_name=None,
        model_api_key=None,
    )
    return dataclasses.replace(settings, **changes)


def _run(
    server: dict[str, Any], settings: ojs_agent.AgentSettings, steps: Callable[[ojs_agent.OjsAgent], Awaitable[Any]]
) -> tuple[str, Any]:
    """What ojs_agent.main() does: install, a session URL, ordinary Stagehand."""

    async def session() -> tuple[str, Any]:
        statelock.client.install()
        url = create_session_url(
            server["base"],
            api_key=agent_key(AGENT),
            saved_session=settings.saved_session,
            save_session=bool(settings.saved_session),
        )
        stagehand = await ojs_agent.start_stagehand(settings, url.cdp_url)
        try:
            async with url.guard(api_key=agent_key(AGENT)):
                return url.session_id, await steps(ojs_agent.OjsAgent(settings, stagehand))
        except StatelockPolicyViolationError as violation:
            return url.session_id, violation
        finally:
            await stagehand.close()
            statelock.client.uninstall()

    return asyncio.run(session())


def test_agent_screens_papers_and_records_a_decline(ojs: dict[str, Any], tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"102": {"reject": True, "email": EMAIL}}), encoding="utf-8")
    settings = _settings(ojs, tmp_path, decisions_file=decisions, record_decisions=True)

    session_id, screenings = _run(ojs, settings, lambda agent: agent.run())
    assert not isinstance(screenings, BaseException), screenings
    checks = {s.paper_id: s.checks for s in screenings}
    assert checks[101] == {"has_manuscript_pdf": True, "has_comments_to_editor": True, "has_contributors": True}
    assert checks[102]["has_comments_to_editor"] is False
    assert checks[103]["has_manuscript_pdf"] is False  # paper.tex: downloaded, then flagged
    assert [s.authors for s in screenings if s.paper_id == 101] == [["Ada Lovelace", "Alan Turing"]]
    assert (tmp_path / "out" / "results.json").exists()

    submission = ojs["journal"].submissions[102]
    assert submission.declined
    assert "Regarding submission 102:" in (submission.decline_message or "")
    assert EMAIL in (submission.decline_message or "")

    records = actions(ojs, session_id)
    assert all(r["verdict"]["decision"] == "allow" for r in records)
    typed_params = [r["context"]["params"] for r in records if r["context"]["method"] == "Input.insertText"]
    typed = [p.get("text") for p in typed_params]
    # Statelock typed the password for the placeholder; the evidence names the secret, not its value.
    assert {"text": "[SECRET]", "statelock_secrets": ["ojs_password"]}.items() <= typed_params[1].items()
    session_dir = ojs["root"] / "artifacts" / "sessions" / session_id
    assert all(mock_ojs.PASSWORD not in str(t) for t in typed[1:])
    assert not any(
        f'"{mock_ojs.PASSWORD}"' in path.read_text(errors="replace") for path in session_dir.rglob("context.json")
    )
    assert EMAIL in typed  # the email sent to the authors is in the evidence
    downloads = [r["context"]["params"] for r in records if r["context"]["method"] == "Statelock.download"]
    completed = {d["suggested_filename"]: d for d in downloads if d.get("phase") == "complete"}
    assert set(completed) == {"manuscript.pdf", "paper.pdf", "paper.tex"}
    assert all(len(d["sha256"]) == 64 for d in completed.values())


def test_stagehand_act_clicks_are_replayed_as_governed_clicks(ojs: dict[str, Any], tmp_path: Path) -> None:
    # Stagehand 0.4 clicks with el.click(); Statelock replays it as a real, checked click.
    comments = ObserveResult(
        selector="xpath=//a[@id='editor-comments']", description="Comments for the Editor", method="click", arguments=[]
    )

    async def steps(agent: ojs_agent.OjsAgent) -> bool:
        await agent.login()
        await agent.open_submission(101)
        await agent.stagehand.page.act(comments)
        await agent.page.locator("#comments-modal").wait_for(state="visible", timeout=5000)
        return True

    session_id, result = _run(ojs, _settings(ojs, tmp_path), steps)
    assert result is True, result
    replayed = [r for r in actions(ojs, session_id) if "statelock_replayed_script_click" in r["context"]["params"]]
    assert [r["context"]["params"]["type"] for r in replayed] == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_prohibited_workflow_button_ends_the_session(ojs: dict[str, Any], tmp_path: Path) -> None:
    send_to_review = ObserveResult(
        selector="xpath=//a[normalize-space()='Send to Review']",
        description="Send to Review",
        method="click",
        arguments=[],
    )

    async def steps(agent: ojs_agent.OjsAgent) -> None:
        await agent.login()
        await agent.open_submission(101)
        await agent.stagehand.page.act(send_to_review)  # the AI chose a button the policy prohibits
        for _ in range(200):  # until the session Statelock ended fails the next call
            await agent.page.title()
            await asyncio.sleep(0.05)

    _, result = _run(ojs, _settings(ojs, tmp_path), steps)
    assert isinstance(result, StatelockPolicyViolationError), result
    assert result.rule == "prohibit_click_text"
    assert "Send to Review" in result.reason


def test_saved_session_skips_the_login_on_the_next_run(ojs: dict[str, Any], tmp_path: Path) -> None:
    settings = _settings(ojs, tmp_path, saved_session="ojs-editor-test")

    async def log_in(agent: ojs_agent.OjsAgent) -> str:
        await agent.login()
        return str(agent.page.url)

    first_id, first = _run(ojs, settings, log_in)
    assert not isinstance(first, BaseException), first
    second_id, second = _run(ojs, settings, log_in)
    assert not isinstance(second, BaseException), second
    assert "/login" not in second

    def secret_typed(session_id: str) -> bool:
        return any(r["context"]["params"].get("statelock_secrets") for r in actions(ojs, session_id))

    assert secret_typed(first_id)  # the first run logged in with the secret
    assert not secret_typed(second_id)  # the second reused the saved login: no password at all


# Each browser check of the policy, broken on purpose ----------------------------------------


@pytest.fixture(autouse=True)
def fresh_journal(ojs: dict[str, Any]) -> Iterator[None]:
    """Every test starts with no faults, no declines and the default decisions."""
    yield
    journal = ojs["journal"]
    journal.faults = mock_ojs.Faults()
    for submission in journal.submissions.values():
        submission.declined, submission.decline_message = False, None
    write_decisions(ojs["decisions"], {102: {"reject": True, "email": EMAIL}})


def _violation(result: Any) -> StatelockPolicyViolationError:
    assert isinstance(result, StatelockPolicyViolationError), result
    return result


def _decline(email: str) -> Callable[[ojs_agent.OjsAgent], Awaitable[Any]]:
    async def steps(agent: ojs_agent.OjsAgent) -> Any:
        agent.settings.output_dir.mkdir(parents=True, exist_ok=True)
        await agent.login()
        screening = await agent.screen(102)
        await agent.decline(screening, email)
        return screening

    return steps


def test_the_wrong_account_is_blocked_before_login(ojs: dict[str, Any], tmp_path: Path) -> None:
    _, result = _run(ojs, _settings(ojs, tmp_path, username="another-editor"), lambda agent: agent.login())
    violation = _violation(result)
    assert (violation.policy_id, violation.rule, violation.violation_type) == (
        "ojs-login",
        "assert_field_equal",
        "pre_condition",
    )
    assert "another-editor" in violation.reason


def test_a_failed_login_is_caught_after_the_click(ojs: dict[str, Any], tmp_path: Path) -> None:
    ojs["journal"].faults.reject_logins = True
    _, result = _run(ojs, _settings(ojs, tmp_path), lambda agent: agent.login())
    violation = _violation(result)
    assert (violation.policy_id, violation.violation_type) == ("ojs-login", "post_condition")


def test_the_all_active_list_must_open(ojs: dict[str, Any], tmp_path: Path) -> None:
    ojs["journal"].faults.broken_tabs = True

    async def steps(agent: ojs_agent.OjsAgent) -> Any:
        await agent.login()
        return await agent.active_submission_ids()

    _, result = _run(ojs, _settings(ojs, tmp_path), steps)
    violation = _violation(result)
    assert (violation.policy_id, violation.rule, violation.violation_type) == (
        "ojs-submissions",
        "assert_compare",
        "post_condition",
    )


def test_a_page_showing_another_paper_is_blocked(ojs: dict[str, Any], tmp_path: Path) -> None:
    ojs["journal"].faults.shown_id_offset = 1  # URL says 101, the page says 102
    _, result = _run(ojs, _settings(ojs, tmp_path), _decline(EMAIL))
    violation = _violation(result)
    assert (violation.policy_id, violation.rule) == ("ojs-workflow", "assert_field_equal")
    assert "url_paper_id (102) did not equal page_paper_id (103)" in violation.reason


def test_the_decision_source_must_still_say_decline(ojs: dict[str, Any], tmp_path: Path) -> None:
    write_decisions(ojs["decisions"], {})  # the editors withdrew the decision; the agent still has it
    _, result = _run(ojs, _settings(ojs, tmp_path, record_decisions=True), _decline(EMAIL))
    violation = _violation(result)
    assert (violation.policy_id, violation.rule) == ("ojs-workflow", "ojs_decision")
    assert "does not say to decline paper 102" in violation.reason
    assert not ojs["journal"].submissions[102].declined


def test_the_email_must_be_the_decided_one(ojs: dict[str, Any], tmp_path: Path) -> None:
    other = "Thank you. We decline your paper because we did not like its title at all."
    _, result = _run(ojs, _settings(ojs, tmp_path, record_decisions=True), _decline(other))
    violation = _violation(result)
    assert (violation.policy_id, violation.rule) == ("ojs-workflow", "ojs_decision")
    assert "not the one the decision source has" in violation.reason
    assert not ojs["journal"].submissions[102].declined


def test_a_short_email_is_blocked_even_if_the_agent_allows_it(
    ojs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ojs_agent, "MIN_EMAIL_CHARS", 0)  # the agent's own check is gone
    _, result = _run(ojs, _settings(ojs, tmp_path, record_decisions=True), _decline("No."))
    violation = _violation(result)
    assert (violation.policy_id, violation.rule) == ("ojs-workflow", "assert_compare")
    assert "decline_email_length" in violation.reason
    assert not ojs["journal"].submissions[102].declined


def test_a_decline_that_is_not_recorded_is_caught(ojs: dict[str, Any], tmp_path: Path) -> None:
    ojs["journal"].faults.ignore_declines = True
    _, result = _run(ojs, _settings(ojs, tmp_path, record_decisions=True), _decline(EMAIL))
    violation = _violation(result)
    assert (violation.policy_id, violation.violation_type) == ("ojs-workflow", "post_condition")


def test_an_unreadable_decision_source_blocks(ojs: dict[str, Any], tmp_path: Path) -> None:
    ojs["decisions"].write_text("{not json", encoding="utf-8")  # the custom rule raises: it fails closed
    _, result = _run(ojs, _settings(ojs, tmp_path), _decline(EMAIL))
    violation = _violation(result)
    assert violation.rule == "ojs_decision"
    assert "could not be evaluated" in violation.reason


def test_a_correct_decline_passes_every_check(ojs: dict[str, Any], tmp_path: Path) -> None:
    session_id, result = _run(ojs, _settings(ojs, tmp_path, record_decisions=True), _decline(EMAIL))
    assert not isinstance(result, BaseException), result
    assert ojs["journal"].submissions[102].declined
    records = actions(ojs, session_id)
    evaluated = [
        e
        for r in records
        for e in (r.get("post_verdict") or {}).get("evidence", {}).get("post_conditions_evaluated", [])
    ]
    triggered = {e["policy_id"] for e in evaluated if e["triggered"]}
    assert triggered == {"ojs-login", "ojs-workflow"}  # the Login and both decline clicks were checked after
