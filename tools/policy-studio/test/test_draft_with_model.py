# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the local Policy Studio draft flow. LiteLLM is always a stub."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import draft_with_model as draft  # noqa: E402
import server  # noqa: E402

BASE_URL = "http://127.0.0.1:8765"
PROMPT = draft.system_prompt()


def summary(**overrides: Any) -> dict:
    return {
        "label": "Clicked Decline Submission",
        "selectors": ["aria/Decline Submission", "#decline-button", 'xpath///*[@id="decline-button"]'],
        "page_url": "http://127.0.0.1:8081/index.php/journal/workflow/102",
        "navigates_to": "",
        "frame": False,
        **overrides,
    }


def block_run(run: int = 2, **overrides: Any) -> dict:
    return {
        "run": run,
        "kind": "block",
        "description": "The page shows submission 101, not the one in the URL.",
        "block_step": {"step": 8, "step_type": "click", "step_summary": summary()},
        **overrides,
    }


def context(*runs: dict) -> dict:
    return {
        "runs": [{"run": 1, "kind": "pass", "description": "", "block_step": None}, *runs],
        "agent_id": "ojs_screening_agent",
        "page_scope": "/index.php/journal/workflow/",
        "notes": [
            {
                "run": 1,
                "step": 8,
                "step_type": "click",
                "phase": "before",
                "text": "The page identifies the right paper.",
                "step_summary": summary(),
            }
        ],
    }


def model_result(
    *, questions: list[str] | None = None, yaml_text: str | None = None, blocks: list[dict] | None = None
) -> dict:
    return {
        "reply": "I need the page field selector." if questions else "I drafted a rule.",
        "questions": questions or [],
        "mappings": [{"note_index": 0, "status": "proposed", "explanation": "Compare the URL and page fields."}],
        "blocks": blocks or [],
        "yaml": yaml_text,
    }


def turn(user: str = "Review these checks.", *, draft_requested: bool = False) -> dict:
    return {"user": user, "assistant": json.dumps(model_result()), "draft_requested": draft_requested}


def replying(content: str) -> Mock:
    return Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))]))


@contextmanager
def studio(completion: Mock | None = None, env: dict[str, str] | None = None) -> Iterator[TestClient]:
    """The app with its lifespan run against a stub LiteLLM, and the page's token on every call."""
    fake = SimpleNamespace(completion=completion or replying(json.dumps(model_result())))
    with (
        patch.dict(os.environ, env if env is not None else {draft.MODEL_KEY_ENV: "test-only-key"}, clear=True),
        patch.dict(sys.modules, {"litellm": fake}),
        TestClient(server.app, base_url=BASE_URL) as client,
    ):
        client.headers[server.TOKEN_HEADER] = server.TOKEN
        yield client


def start() -> None:
    async def run() -> None:
        async with server.lifespan(server.app):
            pass

    asyncio.run(run())


class DraftTests(unittest.TestCase):
    def test_provider_neutral_key_overrides_provider_environment(self) -> None:
        with patch.dict(os.environ, {draft.MODEL_KEY_ENV: "test-only-key", "GEMINI_API_KEY": "wrong-key"}, clear=True):
            for model in ("gemini/gemini-3.8-flash", "openai/test-model", "anthropic/test-model"):
                self.assertEqual(draft.ensure_model_key(model), "test-only-key")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "provider-key"}, clear=True):
            with self.assertRaisesRegex(ValueError, draft.MODEL_KEY_ENV):
                draft.ensure_model_key(draft.DEFAULT_MODEL)
            self.assertIsNone(draft.ensure_model_key("ollama_chat/test-model"))
        with (
            patch.dict(os.environ, {draft.MODEL_KEY_ENV: " test-only-key "}, clear=True),
            self.assertRaisesRegex(ValueError, "surrounding whitespace"),
        ):
            draft.ensure_model_key(draft.DEFAULT_MODEL)

    def test_startup_diagnostics_do_not_log_the_key(self) -> None:
        key = "test-only-private-key"
        with (
            patch.dict(os.environ, {draft.MODEL_KEY_ENV: key}, clear=True),
            patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=Mock())}),
            patch.object(server.metadata, "version", return_value="test-version"),
            self.assertLogs(server.LOGGER, level="INFO") as logs,
        ):
            start()
        output = "\n".join(logs.output)
        self.assertIn("litellm=test-version", output)
        self.assertIn("key_present=True", output)
        self.assertIn(hashlib.sha256(key.encode()).hexdigest()[:12], output)
        self.assertNotIn(key, output)

    def test_key_problems_are_visible_at_startup(self) -> None:
        for env, expected in (
            ({}, "key_present=False key_fingerprint=none"),
            ({}, f"Set {draft.MODEL_KEY_ENV}"),
            ({draft.MODEL_KEY_ENV: " padded "}, "surrounding whitespace"),
        ):
            with (
                self.subTest(expected=expected),
                patch.dict(os.environ, env, clear=True),
                patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=Mock())}),
                self.assertLogs(server.LOGGER, level="INFO") as logs,
            ):
                start()
            self.assertIn(expected, "\n".join(logs.output))

    def test_startup_imports_litellm_and_fails_without_it(self) -> None:
        completion = Mock()
        with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=completion)}):
            start()
        self.assertIs(server.app.state.completion, completion)
        with patch.dict(sys.modules, {"litellm": None}), self.assertRaises(ImportError):
            start()

    def test_startup_fails_when_a_rule_module_cannot_load(self) -> None:
        with (
            patch.object(server, "RULE_MODULES", str(ROOT / "missing_rules.py")),
            patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=Mock())}),
            self.assertRaises(FileNotFoundError),
        ):
            start()

    def test_draft_passes_the_configured_key_and_json_mode_to_litellm(self) -> None:
        payload = {"context": context(), "turns": [], "message": "Review", "draft_requested": False}
        completion = replying(json.dumps(model_result()))
        with studio(completion, {draft.MODEL_KEY_ENV: "test-only-key", "GEMINI_API_KEY": "wrong-key"}) as client:
            for model in (draft.DEFAULT_MODEL, "openai/test-model", "anthropic/test-model"):
                with self.subTest(model=model), patch.object(server, "MODEL", model):
                    self.assertEqual(client.post("/api/draft", json=payload).status_code, 200)
                    kwargs = completion.call_args.kwargs
                    self.assertEqual(kwargs["api_key"], "test-only-key")
                    self.assertEqual(kwargs["model"], model)
                    self.assertEqual(kwargs["response_format"], {"type": "json_object"})
                    self.assertNotIn("api_base", kwargs)
            with patch.object(server, "API_BASE", "http://host.docker.internal:11434"):
                self.assertEqual(client.post("/api/draft", json=payload).status_code, 200)
            self.assertEqual(completion.call_args.kwargs["api_base"], "http://host.docker.internal:11434")

    def test_provider_error_logs_only_safe_codes(self) -> None:
        payload = {"context": context(), "turns": [], "message": "private annotation", "draft_requested": False}
        key = "test-only-private-key"
        error = RuntimeError(f"Provider echoed {key} and private annotation")
        body = [{"error": {"status": "INVALID_ARGUMENT", "message": key, "details": [{"reason": "API_KEY_INVALID"}]}}]
        error.response = SimpleNamespace(status_code=400, text=json.dumps(body))
        with (
            studio(Mock(side_effect=error), {draft.MODEL_KEY_ENV: key}) as client,
            self.assertLogs(server.LOGGER, level="WARNING") as logs,
        ):
            response = client.post("/api/draft", json=payload)
        self.assertEqual(response.status_code, 502)
        output = "\n".join(logs.output) + response.json()["detail"]
        for expected in ("HTTP 400", "API_KEY_INVALID", "INVALID_ARGUMENT"):
            self.assertIn(expected, output)
        self.assertNotIn(key, output)
        self.assertNotIn(payload["message"], output)

    def test_fenced_json_replies_are_unwrapped_and_stored_unwrapped(self) -> None:
        text = json.dumps(model_result(questions=["Which selector?"]))
        for fenced in (f"```json\n{text}\n```", f"```\n{text}\n```\n", f"  ```JSON \n{text}\n```"):
            stored, result = draft.parse_response(fenced, 1, [])
            self.assertEqual(stored, text)
            self.assertEqual(result["questions"], ["Which selector?"])
        for broken in (
            f"```json\n{text}",
            f"Here it is:\n```json\n{text}\n```",
            f"```json\n{text}\n```\n```json\n{text}\n```",
        ):
            with self.assertRaisesRegex(ValueError, "JSON object"):
                draft.parse_response(broken, 1, [])
        payload = {"context": context(), "turns": [], "message": "Review", "draft_requested": False}
        with studio(replying(f"```json\n{text}\n```")) as client:
            response = client.post("/api/draft", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["assistant"], text)
            payload["turns"] = [{"user": "Review", "assistant": response.json()["assistant"], "draft_requested": False}]
            payload["message"] = "One paper."
            self.assertEqual(client.post("/api/preview", json=payload).status_code, 200)

    def test_context_never_accepts_raw_recording(self) -> None:
        value = context()
        value["steps"] = [{"value": "private-password", "url": "https://example.test/private"}]
        with self.assertRaisesRegex(ValueError, "Expected runs, notes"):
            draft.check_context(value)
        self.assertEqual(draft.check_context(context())["notes"][0]["note_index"], 0)

    def test_step_summaries_have_exactly_the_derived_fields(self) -> None:
        def checked(step_type: str = "click", **overrides: Any) -> dict:
            value = context()
            value["notes"][0]["step_type"] = step_type
            value["notes"][0]["step_summary"] = summary(**overrides)
            return draft.check_context(value)["notes"][0]["step_summary"]

        self.assertEqual(checked(), summary())
        self.assertEqual(checked(page_url="", navigates_to="https://a.test/next/")["page_url"], "")
        self.assertEqual(checked("keyDown", key="Enter")["key"], "Enter")
        self.assertEqual(checked("keyUp", key="")["key"], "")
        self.assertEqual(len(checked(selectors=["#a"] * draft.MAX_SELECTORS)["selectors"]), draft.MAX_SELECTORS)
        long_url = "https://a.test/" + "p" * (draft.MAX_URL_CHARS - len("https://a.test/"))
        self.assertEqual(checked(page_url=long_url)["page_url"], long_url)
        for step_type, overrides in (
            ("click", {"value": "typed-secret"}),
            ("click", {"text": "typed-secret"}),
            ("click", {"key": "Enter"}),
            ("keyDown", {}),
            ("keyDown", {"key": "k" * (draft.MAX_KEY_CHARS + 1)}),
            ("click", {"label": ""}),
            ("click", {"label": "x" * (draft.MAX_LABEL_CHARS + 1)}),
            ("click", {"selectors": "#a"}),
            ("click", {"selectors": ["#a"] * (draft.MAX_SELECTORS + 1)}),
            ("click", {"selectors": ["#" + "a" * draft.MAX_SELECTOR_CHARS]}),
            ("click", {"selectors": [" "]}),
            ("click", {"selectors": [["#a"]]}),
            ("click", {"page_url": "https://a.test/login?user=me"}),
            ("click", {"page_url": "https://a.test/login#token"}),
            ("click", {"page_url": "https://me:pw@a.test/"}),
            ("click", {"page_url": "https://a.test/caf\u00e9"}),
            ("click", {"page_url": "https://a.test/a b"}),
            ("click", {"navigates_to": "javascript:alert(1)"}),
            ("click", {"navigates_to": "file:///etc/passwd"}),
            ("click", {"navigates_to": None}),
            ("click", {"page_url": long_url + "p"}),
            ("click", {"frame": "yes"}),
        ):
            with (
                self.subTest(step_type=step_type, overrides=str(overrides)[:60]),
                self.assertRaisesRegex(ValueError, "Annotation 1 is invalid"),
            ):
                checked(step_type, **overrides)
        value = context()
        del value["notes"][0]["step_summary"]
        with self.assertRaisesRegex(ValueError, "Annotation 1 is invalid"):
            draft.check_context(value)

    def test_the_model_sees_the_step_summaries(self) -> None:
        messages = draft.model_messages(draft.check_context(context()), [], "Review", prompt=PROMPT)
        self.assertEqual(json.loads(messages[1]["content"])["notes"][0]["step_summary"], summary())
        self.assertIn("step_summary", messages[0]["content"])

    def test_prompt_lists_every_registered_rule_from_the_code(self) -> None:
        from statelock.policy.rules import RULES  # noqa: PLC0415

        self.assertNotIn(draft.RULES_PLACEHOLDER, PROMPT)
        for name, rule_cls in RULES.items():
            with self.subTest(rule=name):
                line = next(line for line in PROMPT.splitlines() if line.startswith(f"- {name} ("))
                self.assertIn("trigger allowed" if rule_cls.triggerable else "no trigger", line)
                self.assertEqual("pre_conditions" in line, "pre" in rule_cls.phases)
                self.assertEqual("post_conditions" in line, "post" in rule_cls.phases)
                self.assertNotIn("No description.", line)
                for option in set(rule_cls.model_fields) - {"on_fail", "trigger"}:
                    self.assertIn(option, line)
        line = next(line for line in PROMPT.splitlines() if line.startswith("- assert_compare ("))
        self.assertIn("op (required): <|<=|>|>=|==|!=", line)
        self.assertIn("Numeric comparison of a field with another field or a constant.", line)
        self.assertIn(
            "no on_fail: review", next(x for x in PROMPT.splitlines() if x.startswith("- restrict_downloads"))
        )

    def test_prompt_names_every_policy_field_and_option(self) -> None:
        from statelock.policy.fields import FieldSpec  # noqa: PLC0415
        from statelock.policy.models import PolicyConfig  # noqa: PLC0415
        from statelock.policy.triggers import ActionTrigger  # noqa: PLC0415

        for model in (PolicyConfig, FieldSpec, ActionTrigger):
            for name in model.model_fields:
                with self.subTest(model=model.__name__, option=name):
                    self.assertIn(f"`{name}", PROMPT)
        for value in ("`text`", "`count`", "`length`", "`block`", "`review`"):
            self.assertIn(value, PROMPT)
        # General guidance only: nothing about one site's recording.
        self.assertNotIn("OJS", draft.SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("Do not hard-code IDs, names or other values", " ".join(draft.SYSTEM_PROMPT_TEMPLATE.split()))

    def test_custom_rule_modules_appear_in_the_prompt(self) -> None:
        from statelock.policy import extensions  # noqa: PLC0415
        from statelock.policy.rules import RULES  # noqa: PLC0415

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        module = Path(directory.name) / "studio_custom_rules.py"
        module.write_text(
            textwrap.dedent(
                '''
                from typing import ClassVar

                from statelock.policy.rules import Rule, register_rule


                @register_rule
                class StudioCustomRule(Rule):
                    """The invoice is approved in the finance system.

                    More detail that the prompt leaves out.
                    """

                    rule_name: ClassVar[str] = "studio_custom_rule"
                    phases: ClassVar[frozenset[str]] = frozenset({"pre"})
                    triggerable: ClassVar[bool] = False

                    invoice_field: str
                    strict: bool = True
                '''
            ),
            encoding="utf-8",
        )
        payload = {"context": context(), "turns": [], "message": "Review", "draft_requested": False}
        with (
            patch.dict(RULES),
            patch.dict(extensions._loaded),
            patch.object(server, "RULE_MODULES", str(module)),
            studio() as client,
        ):
            prompt = client.post("/api/preview", json=payload).json()["messages"][0]["content"]
        self.assertIn(
            "- studio_custom_rule (pre_conditions; no trigger). Options: invoice_field (required), strict. "
            "The invoice is approved in the finance system.\n",
            prompt,
        )
        self.assertNotIn("More detail", prompt)
        self.assertNotIn("studio_custom_rule", draft.system_prompt())

    def test_chat_turns_are_sent_in_order(self) -> None:
        turns = [turn("All active papers")]
        messages = draft.model_messages(
            draft.check_context(context()), draft.check_turns(turns), "The selector is #paper-id", prompt=PROMPT
        )
        self.assertEqual([entry["role"] for entry in messages], ["system", "user", "user", "assistant", "user"])
        self.assertEqual(json.loads(messages[2]["content"]), {"draft_requested": False, "message": "All active papers"})
        self.assertIn("#paper-id", messages[-1]["content"])
        self.assertIn('"draft_requested": false', messages[-1]["content"])
        self.assertNotIn("existing_policies", json.dumps(messages))

    def test_the_server_adds_the_draft_instruction(self) -> None:
        checked = draft.check_context(context())
        message = "x" * draft.MAX_MESSAGE_CHARS
        current = json.loads(
            draft.model_messages(checked, [], message, prompt=PROMPT, draft_requested=True)[-1]["content"]
        )
        self.assertEqual(current, {"draft_requested": True, "message": message, "instruction": draft.DRAFT_INSTRUCTION})
        current = json.loads(draft.model_messages(checked, [], "", prompt=PROMPT, draft_requested=True)[-1]["content"])
        self.assertEqual(current["message"], "")
        past = draft.model_messages(checked, [turn("", draft_requested=True)], "Thanks", prompt=PROMPT)[2]["content"]
        self.assertEqual(json.loads(past)["instruction"], draft.DRAFT_INSTRUCTION)
        with self.assertRaisesRegex(ValueError, "Write a question"):
            draft.model_messages(checked, [], "  ", prompt=PROMPT)
        with self.assertRaisesRegex(ValueError, f"limited to {draft.MAX_MESSAGE_CHARS} characters"):
            draft.model_messages(checked, [], message + "x", prompt=PROMPT, draft_requested=True)

    def test_agent_id_is_needed_only_for_drafting(self) -> None:
        value = context()
        value["agent_id"] = ""
        checked = draft.check_context(value)
        draft.model_messages(checked, [], "What should I check?", prompt=PROMPT)
        with self.assertRaisesRegex(ValueError, "agent ID before drafting"):
            draft.model_messages(checked, [], "Draft YAML", prompt=PROMPT, draft_requested=True)

    def test_turn_limit_leaves_room_for_the_reply(self) -> None:
        self.assertEqual(len(draft.check_turns([turn()] * (draft.MAX_TURNS - 1))), draft.MAX_TURNS - 1)
        with self.assertRaisesRegex(ValueError, "start a new conversation"):
            draft.check_turns([turn()] * draft.MAX_TURNS)

    def test_saved_turns_must_be_what_the_chat_view_renders(self) -> None:
        good = turn()
        for bad in (
            {**good, "user": ""},
            {**good, "user": "x" * (draft.MAX_MESSAGE_CHARS + 1)},
            {**good, "assistant": ""},
            {**good, "assistant": "not json"},
            {**good, "assistant": "null"},
            {**good, "assistant": json.dumps({"reply": "ok", "questions": "one"})},
            {**good, "assistant": json.dumps({"reply": "x" * draft.MAX_REPLY_CHARS, "questions": []})},
            {**good, "draft_requested": "no"},
            {"user": "hi", "assistant": good["assistant"]},
        ):
            with self.subTest(turn=str(bad)[:80]), self.assertRaisesRegex(ValueError, "Invalid chat history"):
                draft.check_turns([bad])
        self.assertEqual(draft.check_turns([turn("", draft_requested=True)])[0]["user"], "")

    def test_questions_withhold_yaml_and_every_note_is_mapped(self) -> None:
        with self.assertRaisesRegex(ValueError, "while questions remain"):
            candidate = model_result(questions=["Which selector?"], yaml_text="policies: []")
            draft.parse_response(json.dumps(candidate), 1, [])
        with self.assertRaisesRegex(ValueError, "every annotation"):
            candidate = model_result(questions=["Which selector?"])
            candidate["mappings"] = []
            draft.parse_response(json.dumps(candidate), 1, [])
        candidate = model_result(questions=["Which selector?"])
        candidate["mappings"][0]["status"] = "unresolved"
        self.assertEqual(draft.parse_response(json.dumps(candidate), 1, [])[1]["mappings"][0]["status"], "unresolved")
        candidate["questions"] = []
        with self.assertRaisesRegex(ValueError, "without asking questions"):
            draft.parse_response(json.dumps(candidate), 1, [], draft_requested=True)
        self.assertIsNone(draft.parse_response(json.dumps(model_result()), 1, [])[1]["yaml"])
        with self.assertRaisesRegex(ValueError, "during clarification chat"):
            draft.parse_response(json.dumps(model_result(yaml_text="policies: []")), 1, [])

    def test_validates_standalone_policy_and_agent_id(self) -> None:
        import yaml  # noqa: PLC0415

        policy = {
            "policies": [
                {
                    "agent_id": "ojs_screening_agent",
                    "target_url_contains": "/journal/",
                    "fields": [{"name": "paper_id", "selector": "#paper-id"}],
                    "pre_conditions": [{"assert_field_equal": {"left": "paper_id", "value": "102"}}],
                }
            ]
        }
        self.assertEqual(draft.validate_yaml(yaml.safe_dump(policy), "ojs_screening_agent"), 1)
        with self.assertRaisesRegex(ValueError, "wrong agent ID"):
            draft.validate_yaml(yaml.safe_dump(policy), "another_agent")

    def test_preview_and_clarification_round_trip(self) -> None:
        payload = {
            "context": context(),
            "turns": [],
            "message": "Please review these checks.",
            "draft_requested": False,
        }
        payload["context"]["agent_id"] = ""
        with studio(replying(json.dumps(model_result(questions=["One paper or all active papers?"])))) as client:
            preview = client.post("/api/preview", json=payload)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(json.loads(preview.json()["messages"][-1]["content"])["message"], payload["message"])
            sent = json.loads(preview.json()["messages"][1]["content"])
            self.assertEqual(sent["notes"][0]["step_summary"], summary())
            self.assertNotIn("existing_policies", preview.text)
            response = client.post("/api/draft", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"]["questions"], ["One paper or all active papers?"])
            payload["turns"] = [{"user": payload["message"], "assistant": response.json()["assistant"]}]
            payload["turns"][0]["draft_requested"] = False
            payload["message"] = "All active papers."
            self.assertIn("All active papers", client.post("/api/preview", json=payload).text)
            payload["draft_requested"] = True
            self.assertEqual(client.post("/api/preview", json=payload).status_code, 400)
            payload["context"]["agent_id"] = "ojs_screening_agent"
            self.assertEqual(client.post("/api/preview", json=payload).status_code, 200)

    def test_api_needs_the_page_token_a_loopback_host_and_same_origin(self) -> None:
        payload = {"context": context(), "turns": [], "message": "Draft", "draft_requested": False}
        with studio() as client:
            self.assertEqual(client.post("/api/preview", json=payload).status_code, 200)
            for headers in (
                {server.TOKEN_HEADER: ""},
                {server.TOKEN_HEADER: "wrong"},
                {server.TOKEN_HEADER: server.TOKEN + "x"},
                {"Origin": "https://other.test"},
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(client.post("/api/draft", json=payload, headers=headers).status_code, 403)
            del client.headers[server.TOKEN_HEADER]
            self.assertEqual(client.post("/api/draft", json=payload).status_code, 403)
            client.headers[server.TOKEN_HEADER] = server.TOKEN
            for host in ("testserver", "policy-studio:8765", "evil.test"):
                with self.subTest(host=host):
                    self.assertEqual(client.post("/api/draft", json=payload, headers={"Host": host}).status_code, 400)
            self.assertEqual(
                client.post("/api/preview", json=payload, headers={"Host": "localhost:8765"}).status_code, 200
            )
            payload["context"]["recording"] = {"value": "secret"}
            self.assertEqual(client.post("/api/preview", json=payload).status_code, 400)

    def test_page_serves_its_token_and_limits_with_security_headers(self) -> None:
        with studio() as client:
            page = client.get("/")
            api_error = client.post("/api/preview", content=b"{}", headers={server.TOKEN_HEADER: ""})
            asset = client.get("/studio.js")
        self.assertEqual(page.status_code, 200)
        token = re.search(r'name="statelock-studio-token" content="([^"]+)"', page.text)
        limits = re.search(r'name="statelock-studio-limits" content="([^"]+)"', page.text)
        self.assertEqual(token and token.group(1), server.TOKEN)
        self.assertEqual(limits and json.loads(html.unescape(limits.group(1))), draft.LIMITS)
        self.assertNotIn("{{", page.text)
        for response in (page, api_error, asset):
            headers = response.headers
            self.assertIn("default-src 'self'", headers["content-security-policy"])
            self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            self.assertEqual(headers["referrer-policy"], "no-referrer")
            self.assertEqual(headers["cache-control"], "no-store")
        # The CSP allows no inline code, so the page must have none.
        index = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(index, r"<script(?![^>]*\bsrc=)|<style|\sstyle=|\son[a-z]+=")

    def test_oversized_bodies_are_refused_with_or_without_content_length(self) -> None:
        def chunks() -> Iterator[bytes]:
            for _ in range(20):
                yield b" " * 100

        with studio() as client, patch.object(server, "MAX_BODY_BYTES", 1000):
            self.assertEqual(client.post("/api/preview", content=b" " * 1001).status_code, 413)
            self.assertEqual(client.post("/api/preview", content=chunks()).status_code, 413)
            self.assertEqual(
                client.post("/api/preview", headers={"Content-Length": "x"}, content=b"{}").status_code, 413
            )

    def test_the_largest_valid_request_fits_the_body_cap(self) -> None:
        # JSON.stringify spends its maximum, 6 bytes, on a control character (\u0001) or a
        # lone surrogate (\ud800); a reply must stay parseable, so it uses surrogates.
        worst = "\x01"
        shell = '{"reply": "", "questions": []}'
        reply = shell[:11] + "\ud800" * (draft.MAX_REPLY_CHARS - len(shell)) + shell[11:]
        # Clean URLs are printable ASCII; a quote is the most a JSON encoder spends on one (\").
        url = "https://a/" + '"' * (draft.MAX_URL_CHARS - len("https://a/"))
        step_summary = {
            "label": worst * draft.MAX_LABEL_CHARS,
            "selectors": [worst * draft.MAX_SELECTOR_CHARS] * draft.MAX_SELECTORS,
            "page_url": url,
            "navigates_to": url,
            "frame": False,
        }
        note = {
            "step": draft.MAX_STEPS,
            "step_type": worst * draft.MAX_STEP_TYPE_CHARS,
            "phase": "before",
            "step_summary": step_summary,
        }
        block = {"step": draft.MAX_STEPS, "step_type": note["step_type"], "step_summary": step_summary}
        runs = [{"run": 1, "kind": "pass", "description": worst * draft.MAX_NOTE_CHARS, "block_step": None}] + [
            {"run": run, "kind": "block", "description": worst * draft.MAX_NOTE_CHARS, "block_step": block}
            for run in range(2, draft.MAX_RUNS + 1)
        ]
        payload: dict[str, Any] = {
            "context": {
                "runs": runs,
                "notes": [{**note, "run": draft.MAX_RUNS, "text": worst * draft.MAX_NOTE_CHARS}] * draft.MAX_NOTES,
                "agent_id": worst * draft.MAX_AGENT_ID_CHARS,
                "page_scope": worst * draft.MAX_PAGE_SCOPE_CHARS,
            },
            "turns": [
                {"user": worst * draft.MAX_MESSAGE_CHARS, "assistant": reply, "draft_requested": False}
                for _ in range(draft.MAX_TURNS - 1)
            ],
            "message": worst * draft.MAX_MESSAGE_CHARS,
            "draft_requested": False,
        }
        self.assertEqual(len(draft.check_turns(payload["turns"])), draft.MAX_TURNS - 1)
        self.assertEqual(len(draft.check_context(payload["context"])["notes"]), draft.MAX_NOTES)
        # Compact separators, as the page sends it; these characters escape the same either way.
        size = len(json.dumps(payload, separators=(",", ":")).encode())
        self.assertGreater(size, draft.MAX_BODY_BYTES * 0.9)
        self.assertLessEqual(size, draft.MAX_BODY_BYTES)

    def test_recordings_are_runs_that_must_pass_or_be_blocked(self) -> None:
        checked = draft.check_context(context(block_run()))
        self.assertEqual([run["kind"] for run in checked["runs"]], ["pass", "block"])
        self.assertEqual(checked["runs"][1]["block_step"]["step_summary"], summary())
        note = context()["notes"][0]
        for runs, error in (
            ([], "1 to 10 recordings"),
            ([block_run(1)], "at least one recording that must pass"),
            ([{"run": 1, "kind": "pass", "description": "", "block_step": None}] * 2, "Recording 2 is invalid"),
            ([{"run": 1, "kind": "pass", "description": "", "block_step": block_run()["block_step"]}], "no block step"),
            ([{"run": 1, "kind": "maybe", "description": "", "block_step": None}], "Recording 1 is invalid"),
            ([{"run": True, "kind": "pass", "description": "", "block_step": None}], "Recording 1 is invalid"),
            ([{"run": 1, "kind": "pass", "description": "", "block_step": None, "title": "x"}], "Recording 1 is"),
        ):
            with self.subTest(runs=str(runs)[:60]), self.assertRaisesRegex(ValueError, error):
                draft.check_context({**context(), "runs": runs, "notes": [note]})
        for overrides in (
            {"description": " "},
            {"description": "x" * (draft.MAX_NOTE_CHARS + 1)},
            {"block_step": None},
            {"block_step": {"step": 0, "step_type": "click", "step_summary": summary()}},
            {"block_step": {"step": 8, "step_type": "click", "step_summary": summary(value="typed-secret")}},
            {"block_step": {"step": 8, "step_type": "click", "step_summary": summary(), "value": "typed-secret"}},
        ):
            with self.subTest(overrides=str(overrides)[:60]), self.assertRaisesRegex(ValueError, "Recording 2"):
                draft.check_context(context(block_run(**overrides)))
        for run in (0, 3, True):
            value = context(block_run())
            value["notes"][0]["run"] = run
            with self.subTest(run=run), self.assertRaisesRegex(ValueError, "Annotation 1 is invalid"):
                draft.check_context(value)
        self.assertIn("block_step", PROMPT)

    def test_every_blocked_recording_is_explained(self) -> None:
        explained = {"run": 2, "status": "proposed", "explanation": "The paper id check fails on 101."}
        _, result = draft.parse_response(json.dumps(model_result(blocks=[explained])), 1, [2])
        self.assertEqual(result["blocks"], [explained])
        for blocks in (
            [],
            [explained, explained],
            [{**explained, "run": 1}],
            [{**explained, "status": "maybe"}],
            [{**explained, "explanation": " "}],
            [{**explained, "run": True}],
        ):
            with self.subTest(blocks=blocks), self.assertRaisesRegex(ValueError, "block explanations|every recording"):
                draft.parse_response(json.dumps(model_result(blocks=blocks)), 1, [2])
        candidate = model_result(blocks=[{**explained, "status": "unresolved"}])
        with self.assertRaisesRegex(ValueError, "without asking questions"):
            draft.parse_response(json.dumps(candidate), 1, [2], draft_requested=True)
        candidate = model_result()
        del candidate["blocks"]
        with self.assertRaisesRegex(TypeError, "no block explanations"):
            draft.parse_response(json.dumps(candidate), 1, [])
        # The server asks for an answer for each recording that must be blocked.
        payload = {"context": context(block_run()), "turns": [], "message": "Review", "draft_requested": False}
        with studio(replying(json.dumps(model_result()))) as client:
            self.assertEqual(client.post("/api/draft", json=payload).status_code, 502)
        with studio(replying(json.dumps(model_result(blocks=[explained])))) as client:
            self.assertEqual(client.post("/api/draft", json=payload).json()["result"]["blocks"], [explained])

    def test_generated_yaml_must_pass_schema_before_download(self) -> None:
        payload = {"context": context(), "turns": [], "message": "", "draft_requested": True}
        generated = model_result(yaml_text="policies:\n  - agent_id: ojs_screening_agent\n    unknown_rule: true\n")
        with studio(replying(json.dumps(generated))) as client:
            response = client.post("/api/draft", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["result"]["yaml"])
        self.assertTrue(response.json()["validation_error"])
        self.assertIn("unknown_rule", response.json()["assistant"])


if __name__ == "__main__":
    unittest.main()
