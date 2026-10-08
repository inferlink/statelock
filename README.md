# Statelock

Statelock governs the browser actions of AI agents. It is a Chrome DevTools Protocol (CDP) proxy between your agent (Playwright, browser-use, Stagehand, LangChain or any CDP client) and a headless Chromium. Before each click, keystroke, upload or download it captures the page and checks your policy; it releases or blocks the action; after commit actions it checks post-conditions; and it records evidence for every decision. An agent can then work in a real web portal inside clear limits: it may decline a manuscript but not send it to review, or mark an invoice as paid only when the bank deposit matches.

## Quick start

Install it (the package is `statelock-ai` and imports as `statelock`; the PyPI package named `statelock` is an unrelated project):

```bash
pip install statelock-ai
python -m playwright install chromium
```

Write a policy and a key for your agent. This policy blocks "Mark as Paid" on the built-in finance demo when the bank deposit and the invoice differ:

```bash
cat > policy.yaml <<'EOF'
policies:
  - agent_id: finance_agent
    target_url_contains: /demo/finance
    pre_conditions:
      - assert_field_equal: {left: bank_deposit_amount, right: erp_invoice_amount}
EOF
statelock keygen --agent-id finance_agent --append keys.yaml   # prints the agent's key; keys.yaml holds only its hash
```

Start Statelock with the demo pages on:

```bash
STATELOCK_POLICY_FILE=policy.yaml STATELOCK_AUTH_KEYS_FILE=keys.yaml STATELOCK_ARTIFACT_DIR=artifacts \
  STATELOCK_DEMO=1 statelock --port 8010
```

In another shell, run an agent that tries to mark a mismatched invoice as paid:

```bash
export STATELOCK_URL=http://localhost:8010 STATELOCK_API_KEY=slk_...   # the key keygen printed
cat > agent.py <<'EOF'
import asyncio

from playwright.async_api import async_playwright

import statelock.client


async def main() -> None:
    async with async_playwright() as p:
        async with await statelock.client.connect_playwright(p) as governed, governed.guard():
            await governed.page.goto("http://localhost:8010/demo/finance?scenario=mismatch")
            await governed.page.click("text=Mark as Paid")


asyncio.run(main())
EOF
python agent.py
# StatelockPolicyViolationError: [pre_condition/assert_field_equal] Blocked action because
# bank_deposit_amount ($4,900.00) did not equal erp_invoice_amount ($5,000.00) ...
```

The click never reached the page. `artifacts/sessions/<session id>/` holds the evidence: the page, the values checked, the verdict and a screenshot.

For sync Playwright, browser-use, LangChain, CrewAI, Stagehand and JavaScript agents, see [Connecting an agent](https://github.com/inferlink/statelock/blob/main/README.md#connecting-an-agent).

### With Docker

From a checkout of this repository, start the development proxy and run the two finance cases. The development stack supplies demo keys and enables the demo pages; no local Python installation or key generation is needed.

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build statelock
docker compose -f compose.yaml -f compose.dev.yaml --profile finance-agent run --rm finance-agent
docker compose -f compose.yaml -f compose.dev.yaml --profile finance-pass-agent run --rm finance-pass-agent
```

The first agent is blocked because the deposit is $4,900 and the invoice is $5,000; the second completes when they match. The rule is in [policies/default.yaml](policies/default.yaml), under `finance_reconciliation_agent`: it compares `bank_deposit_amount` with `erp_invoice_amount` before the action and checks for "Reconciliation complete" afterward. See [Policies](#policies) to write your own.

For the test suite, base-proxy Docker setup and other demos, see [Development](#development), [compose.dev.yaml](compose.dev.yaml) and the [OJS walkthrough](examples/ojs/README.md).

## How it works

1. **Admission.** The agent connects with a single-use session URL, or to `ws://<host>/statelock` with `Authorization: Bearer <key>`, `x-statelock-agent-id` (optional; it must match the key), and optionally `x-statelock-session-id` (a UUID). A missing, unknown, disabled or expired key, a key for another agent, or an agent without a policy is refused (`4401`), as are invalid or reused session IDs (`4409`).
2. **Governed Chromium.** Each session gets its own Chromium, started with a minimal environment. CDP is forwarded in both directions; only valid JSON is forwarded, exactly as Statelock evaluated it. Commands that would bypass governance are refused:
   - wrapped commands (`Target.sendMessageToTarget`) and `Debugger.setScriptSource`;
   - loading anything but an http(s) URL or `about:blank` (`javascript:`, `file:`, `data:`, `blob:`, `chrome:`, `view-source:`);
   - `Target.exposeDevToolsProtocol`, `Network.replayXHR` and `Page.setDocumentContent`;
   - commands that reach Statelock's own isolated worlds;
   - agent network interception (`page.route`, any `Fetch.*`).
3. **Page guard.** Installed on every page and iframe before the agent's commands reach it. On governed pages (URLs your policies cover) it:
   - cancels synthetic clicks, submits, drops and pastes (`element.click()`, `dispatch_event`);
   - clears files that code puts into file inputs (including Playwright's own `set_input_files` over remote CDP; `statelock.client.install()` routes it through Statelock, see [Files](https://github.com/inferlink/statelock/blob/main/README.md#files));
   - blocks page loads and requests started by the agent's own page code;
   - blocks `form.submit()` called from code.
4. **Governed input.** For each input command (mouse, key, text, touch, drag and drop, IME, gestures, and `DOM.setFileInputFiles`) Statelock:
   - captures state from the command's tab, in its own isolated world: URL, text, the element under the pointer or focused, fields, accessibility tree, screenshot;
   - evaluates pre-conditions;
   - forwards or blocks the command;
   - for commit actions (mouse release, touch end, tap, drop, Enter release), holds the browser's response, evaluates post-conditions, then releases or blocks.
5. **Attribution.** Code the agent sends (`Runtime.evaluate`, `Runtime.callFunctionOn`, ...) is tagged, so every request can be attributed to `agent_code`, `agent_navigation`, `site` or `unknown`. Real input and the site's own code are never blocked by the guard, so multi-step tasks work normally.
6. **Evidence.**
   - One record per action, in sequence order: `artifacts/sessions/<id>/action-XXXX/context.json` plus screenshots.
   - A request log per session: `network.jsonl`.
   - Redacted: injected secrets, SSNs, card-like numbers that pass the Luhn check, and emails.
   - Written to `STATELOCK_ARTIFACT_DIR`; with Docker, this repo's `./artifacts`. Plain JSON: tamper evidence comes from [Statelock Enclave](https://github.com/inferlink/statelock/blob/main/README.md#statelock-enclave).
7. **Violations.** The agent receives an in-band CDP error and the session closes (`4403` pre-condition, `4412` post-condition). `GET /violations/<session_id>` serves the details, and the client SDK raises `StatelockPolicyViolationError`.

See [COVERAGE.md](https://github.com/inferlink/statelock/blob/main/COVERAGE.md) for what is and is not analyzed.

## Policies

```yaml
policies:
  - id: finance-reconciliation                 # optional; names the policy in verdicts and evidence
    agent_id: finance_reconciliation_agent     # required
    target_url_contains: /demo/finance          # omit to apply to every page
    allow_synthetic_events: false               # true disables the page guard for this policy
    pre_conditions:                             # one rule per entry, optionally with a trigger
      - require_page_text: {values: [Bank Deposit, ERP Invoice]}
      - assert_field_equal: {left: bank_deposit_amount, right: erp_invoice_amount}
      - prohibit_click_text: {values: [Delete, Void]}
      - trigger: {click_text: [Mark as Paid]}   # only just before that button is pressed
        assert_compare: {left: erp_invoice_amount, op: "<=", value: 10000}
    post_conditions:                            # the same, checked after the action
      - trigger: {click_text: [Mark as Paid]}   # or key: [Enter]
        require_page_text: {values: [Reconciliation complete]}
    cookie_access:                              # optional; default: the agent reads no cookies
      names: [csrftoken]                        # exact names; or domains: [...]; or all: true
```

`target_url_contains` matches the page URL as `scheme://host[:port]/path`, without query, fragment or user info.

A `trigger` limits an entry to particular actions. It goes in the entry beside the rule, not inside the rule's options. On a pre-condition it runs only when an element whose text or aria-label contains a `click_text` value is activated (pressed, touched, tapped, dropped on, or Enter/Space while focused), or on the key down of a listed `key`: it checks the page just before the button acts. On a post-condition it runs after that action, on the page it led to. Without a trigger, pre-conditions run on every action on the policy's pages and post-conditions after every click or Enter. An unresolved click target counts as a match (fail closed). `restrict_uploads` and `restrict_downloads` take no trigger.

`id` names the policy in verdicts, violations and the evidence (`policy_id`), so with several policies for one agent the record says which one blocked. Without it the id is `<agent_id>#<n>`: the agent's n-th policy in the file (so reordering the file renames the defaults; set `id` where it matters). Ids are unique in the file.

`cookie_access` applies to the agent (the union of its policies). Statelock returns only the listed cookies to `context.cookies()`, `storage_state()` and `page.request`, and records their names, never their values. `all: true` hands over the site's session cookies, which lets agent-side HTTP act as the user outside Statelock; use it knowingly.

A policy, keys or secrets file with a repeated key at any level (two `pre_conditions:`, two `agents:` sections) is refused when it loads.

### Agent-side HTTP (request_access)

An agent that needs HTTP (a site's JSON API, a file behind a link) can have Statelock make the request. Statelock runs it in the page, with the browser's session, so the agent needs no cookies:

```yaml
    request_access:                             # optional; default: no requests
      - url_pattern: "^https://portal\\.example\\.com/api/"   # regex searched in the full URL
        methods: [GET, POST]                    # default [GET]
        max_response_bytes: 5000000             # default 8 MB, at most 11 MB
```

```python
statelock.client.install()                                   # then Playwright's own page.request works:
invoices = await (await page.request.get("https://portal.example.com/api/invoices")).json()
# or without install(): await statelock_fetch(page, url, method="POST", data={...})
```

- **Patterns.** `url_pattern` must start with `^https://`, `^http://` or `^https?://`, then a literal host (escape dots as `\.`) and `/`. Wildcard hosts are rejected.
- **Where it runs.** Statelock makes the request with `fetch()` in an isolated world of the page, as the site's own code would, so only same-origin or CORS-allowed URLs work. The browser's cookies go with it; the agent never receives them.
- **Recorded.** Each request is an action record: URL, method, header names, body size, status, and the response's size and SHA-256. It shows as `statelock_request` in the network log.
- **Declined.** A request no rule allows is answered with an error (`request_access`, `StatelockRequestError`) and recorded. The session continues.
- **Redirects.** They fail before the destination request is sent. `follow_redirects: true` is rejected during policy validation because the destination cannot be checked before the browser follows it.
- **Secrets.** Header values may use `{{secret:name}}` (for example `Authorization: Bearer {{secret:api_token}}`). The secret needs `password_fields_only: false`, and the request URL must be in its scope (see [Credentials](https://github.com/inferlink/statelock/blob/main/README.md#credentials)). A placeholder that is not allowed ends the session (`secret_injection`), and the value is removed from the response if the endpoint echoes it.
- **Concurrent.** A request runs alongside the agent's other commands.
- **Not supported** through `page.request`: `multipart`, `max_redirects`, `max_retries`, `ignore_https_errors`.

### Fields and remembered values

Rules read named fields. A field is either `data-statelock-key` / `data-statelock-value` markup on the page, or a policy `fields` entry, which works on sites you cannot change:

```yaml
  - agent_id: portal_reconciliation_agent
    target_url_contains: /demo/erp
    fields:
      - name: bank_deposit
        url_contains: /demo/bank        # default: the policy's target_url_contains
        allowed_origins: ["http://statelock:8000", "http://localhost:8010"]
        selector: "#deposit-amount"     # CSS selector; or key: <data-statelock-key>
        # attribute: title              # read an attribute instead of the text
        # pattern: "\\$([0-9,.]+)"      # optional regex; group 1 is the value
        remember: true                  # keep the latest value for this session
      - name: erp_invoice
        selector: "#invoice-amount"
      - {name: invoice_id, from_url: true, pattern: "/invoice/(\\d+)"}   # from the page URL
      - {name: open_dialogs, selector: ".modal", visible: true, read: count}
      - {name: note_length, selector: "body", frame: "iframe#note-editor", read: length}
    pre_conditions:
      - assert_field_equal: {left: remembered.bank_deposit, right: erp_invoice}
      - assert_compare: {left: erp_invoice, op: "<=", value: 10000, on_fail: review}
      - restrict_uploads: {extensions: [.pdf], max_bytes: 5000000, max_files: 1}
```

A field reads one of: `selector` (the first match's text, or `attribute`), `key` (`data-statelock-key` markup), or `from_url: true` (the page URL; use `pattern`). A policy field is read only as the policy defines it, never from markup of the same name. `url_contains` matches like `target_url_contains`. Selector fields also take:

- `frame`: a same-origin iframe to look in (a rich-text editor's body, say);
- `visible: true`: only elements that are shown;
- `read: count` (how many elements match, `0` for none) or `read: length` (characters of the value). A password field is never read; `read: length` is the only thing Statelock takes from one.

`remembered.<name>` is the latest value of a `remember: true` field seen anywhere in the session (any tab). A remembered field must name trusted origins with `allowed_origins`, or use an absolute `url_contains` scope; a path alone could be copied on another site. Statelock captures it at each governed action, when a matching page finishes loading (and again 0.5 s later), and before the agent navigates away (goto, reload, back/forward). Each action record stores the remembered values with their URL, source and time, and a rule that needs a missing value blocks.

Built-in rules:

| Rule | Where it can be used | What it checks |
|---|---|---|
| `require_page_text` | pre and post | The page text contains each listed value. |
| `prohibit_page_text` | pre and post | The page text contains none of the listed values (an error message, a login form after logging in). |
| `assert_field_equal` | pre and post | Two fields (or remembered values) hold the same value, or a field equals a fixed `value`. Numbers compare as numbers; text compares after Unicode NFKC and case folding, ignoring only whitespace and punctuation. Amounts in different currency symbols or currency codes are never equal. |
| `assert_compare` | pre and post | A numeric comparison (`<`, `<=`, `>`, `>=`, `==`, `!=`) of a field with another field (`right`) or a constant (`value`). |
| `prohibit_click_text` | pre only | The clicked or touched element, or the focused element for Enter/Space, does not contain a listed text. |
| `restrict_downloads` | pre only | Downloads the page starts: allowed `extensions`, `name_pattern`, source `url_pattern` and `max_downloads` (per session) when the download begins; `max_bytes` when it completes. A blocked download is cancelled or deleted and ends the session. |
| `restrict_uploads` | pre only | Governed uploads: allowed `extensions`, `max_bytes` per file, `max_files` per upload, `name_pattern`. Other actions pass. |
| `visual_assert` | pre and post | A vision-language model judges the screenshot and can read values off it; `cross_check` compares those with page fields. Fails closed (see below). |

- **Text rules.** `require_page_text`, `prohibit_page_text` and `prohibit_click_text` need at least one non-empty value.
- **Numbers.** `$5,000.00`, `-$5`, `$-5`, `−5` (Unicode minus) and `(1,234.00)` (negative) parse. Ambiguous formats such as `5.000,00`, `5-` or `12.50)` do not, and the rule blocks.
- **Loading.** Entries with zero or several rules, unknown rules, and unknown fields are rejected when the policy file loads. A rule that raises or does not answer within its time limit (30 s by default) blocks the action.

### Custom rules

When a check needs more than page fields (a lookup in the system that holds the decision, a call to another service), write it as a rule and name it in the policy like a built-in one:

```python
from typing import ClassVar
from statelock.policy.rules import Rule, RuleContext, RuleFailure, register_rule

@register_rule
class ApprovedInvoice(Rule):
    rule_name: ClassVar[str] = "approved_invoice"
    phases: ClassVar[frozenset[str]] = frozenset({"pre"})
    check_timeout: ClassVar[float | None] = 10.0
    invoice_field: str

    async def check(self, ctx: RuleContext) -> RuleFailure | None:
        invoice = ctx.field(self.invoice_field)
        if not await is_approved(invoice):   # your lookup; asyncio.to_thread for blocking I/O
            return RuleFailure(f"Blocked: invoice {invoice} is not approved", ctx.field_evidence(self.invoice_field))
        return None
```

```yaml
      - trigger: {click_text: [Mark as Paid]}
        approved_invoice: {invoice_field: invoice_id}
```

Statelock loads rule modules at startup from the `statelock.rules` entry point group (a package that declares `[project.entry-points."statelock.rules"]`) and from `STATELOCK_RULE_MODULES` (module names or `.py` paths). Rules run inside the proxy, installed by whoever runs it; an agent cannot add one. A rule name can be registered once. `examples/ojs/ojs_rules.py` is a worked example: it re-checks the editors' decision before a paper is declined.

### Visual checks (visual_assert)

`visual_assert` asks a vision-language model (VLM) about the screenshot Statelock captured for the action (pre-conditions) or after it (post-conditions):

```yaml
    pre_conditions:
      - visual_assert:
          check_name: amounts_match
          instruction: The bank deposit and the ERP invoice show the same amount.
          extract: {bank_deposit: string}               # optional: values the model reads off the page
          cross_check:                                  # optional: its reading must equal a page field
            - {extracted: bank_deposit, field: bank_deposit_amount}
          # when: activation                            # default: presses, taps, drops, Enter/Space; or: all
```

- **Model.** Any vision model litellm supports (`pip install "statelock-ai[vlm]"`), hosted or self-hosted:

  | Setup | `STATELOCK_PERCEPTION_MODEL` | `STATELOCK_PERCEPTION_API_BASE` |
  | --- | --- | --- |
  | Hosted, e.g. the Gemini your agent already uses | `gemini/gemini-3.5-flash` (key in `STATELOCK_PERCEPTION_API_KEY`) | none |
  | Ollama on the same machine | `ollama_chat/qwen2.5vl:7b` | `http://localhost:11434` |
  | vLLM on a GPU server in your network | `hosted_vllm/Qwen/Qwen2.5-VL-7B-Instruct` | `http://gpu:8000/v1` |

- **Tested models** (the finance screenshots in `tests/fixtures/perception`: one where the amounts match, one where they differ). Gemini 3.5 Flash judged both correctly and read all four amounts, in 3–5 s per check. Qwen2.5-VL 3B through Ollama, on the CPU in Docker on a Mac, read all four amounts correctly, but called the mismatch a match, in 30–40 s per check. A model that small is not reliable for judgments: self-hosted, use a strong vision model on a GPU, and run `tests/test_perception_model.py` against it before relying on it.
- **What a hosted model receives.** A screenshot of the page at every checked action, with the instruction. That can include customer data, and a secret typed into a field that is not a password field. Use a hosted model only where sending those screenshots to the provider is acceptable.
- **Local only.** `STATELOCK_PERCEPTION_LOCAL_ONLY=true` (for air-gapped and regulated deployments) makes Statelock refuse to start unless the model is self-hosted and on this machine or a private network. The model needs a self-hosted provider prefix (`ollama`, `ollama_chat`, `hosted_vllm`, `openai` for an OpenAI-compatible server, `lm_studio`), because a hosted provider can route to its own cloud. `STATELOCK_PERCEPTION_API_BASE` must be local: loopback, RFC 1918 and Docker networks, link-local, unique-local IPv6, or an overlay network such as Tailscale (100.64.0.0/10). A host name must resolve, and every address it resolves to must be local. This is checked once, at startup. It also stops litellm from downloading its model price list.
- **Strict output.** The model must answer `{"passed", "reason", "extracted"}` matching a strict JSON schema (sent as `response_format`, temperature 0). Output is validated with no type coercion and no extra keys.
- **Fails closed.** No model configured, no screenshot, a timeout (`STATELOCK_PERCEPTION_TIMEOUT`), a model error, or invalid output after `STATELOCK_PERCEPTION_RETRIES` more attempts: the rule fails with the reason. A failure blocks, or pauses for review with `on_fail: review`.
- **Cross-checks.** A model can misread or be fooled by the page. `cross_check` compares each value it read with a DOM field (numbers as numbers), so a visual check can back up, not replace, a deterministic one.
- **Cost.** One model call per checked action: pre-conditions check activations only unless `when: all`. The same screenshot and question are answered from a cache. The agent's action waits for the model, so set its action timeouts above `STATELOCK_PERCEPTION_TIMEOUT`.
- **Evidence.** The action record has the model's verdict, what it read, the model name, attempts and latency (`evidence.perception`, `evidence.cross_check`).
- **Try a model:** `statelock perception-check screenshot.jpg -i "The amounts match." -x bank_deposit=string`. `tests/test_perception_model.py` runs the finance fixtures (`tests/fixtures/perception`) against the configured model.

### Human review

Any pre-condition rule takes `on_fail`: `block` (the default) ends the session; `review` pauses the action until a reviewer approves or denies it.

- Only activations wait: a mouse press, a touch start, a tap, a drop, a file upload, and Enter, Space or a Ctrl/Alt/Meta shortcut. The approval covers the matching release, so one click is one review. Other actions (moves, scrolls, plain typing) continue; the failure is recorded as `review_deferred`.
- A failing `on_fail: block` rule still blocks at once, without a review.
- On approval, Statelock captures the page again. If a blocking rule now fails, or a reviewed rule fails differently (for example, the amount changed), the action is blocked.
- A denial, or no decision within `STATELOCK_REVIEW_TIMEOUT` (300 s), blocks the action and ends the session. The violation carries the review.
- `on_fail: review` is refused in post-conditions (the action already ran) and on `restrict_downloads` (a download cannot wait). A download whose page fails a review rule is blocked.
- The action record stores a `review` block (id, status, reviewer, comment, times, failed rules) and `review-screenshot.jpg`, the page the reviewer judged.
- Agents: Playwright's default 30 s action timeout ends before a person answers. Raise it (`context.set_default_timeout(...)`) above the review timeout.

Reviewers use `http://<proxy>/review`, or the API with a reviewer key (`Authorization: Bearer`):

| Endpoint | Purpose |
|---|---|
| `GET /reviews` | Pending reviews of the reviewer's tenant (`?status=approved\|denied\|expired\|cancelled\|all`). |
| `GET /reviews/<id>` | One review: the paused action, page, fields, remembered values, failed rules. |
| `GET /reviews/<id>/screenshot` | The page when the action was paused (JPEG). |
| `POST /reviews/<id>/decision` | `{"decision": "approve" \| "deny", "comment": "..."}`. 409 if already decided or expired. |

Events `review_requested` and `review_decided` let plugins notify reviewers (for example in chat).

## Authentication

Agents authenticate with API keys (`STATELOCK_AUTH_MODE=api_key`, the default). The keys file stores only SHA-256 hashes. `keygen --append` adds each entry under its section (and creates the file if it is missing); the key is printed on stderr:

```bash
statelock keygen --agent-id finance_reconciliation_agent --tenant acme --append keys.yaml
statelock keygen --reviewer alice --tenant acme --append keys.yaml      # a reviewer key (reviewers: section)
statelock keygen --auditor audit-team --tenant acme --append keys.yaml  # an auditor key (auditors: section)
statelock hash-key < key.txt                                            # hash an existing key
```

Without `--append`, `keygen` prints a complete keys file on stdout. Do not add that to an existing file with `>>`: a second `agents:` section makes the file invalid.

```yaml
agents:
  - agent_id: finance_reconciliation_agent
    tenant: acme                    # recorded with every action and violation
    key_sha256: 3b1f...
    expires: 2027-01-01T00:00:00Z   # optional
    disabled: false                 # optional; several entries per agent allow key rotation
reviewers:
  - reviewer_id: alice
    tenant: acme                    # reviews only this tenant's sessions
    key_sha256: 9c0d...
auditors:                           # for plugins that serve evidence
  - auditor_id: audit-team
    tenant: acme                    # reads only this tenant's evidence
    key_sha256: 51ab...
```

Each key has one role. Agent keys cannot use the review API, reviewer keys cannot open sessions, and auditor keys only read evidence through plugin routes.

- The proxy needs `STATELOCK_AUTH_KEYS_FILE` and fails at startup without it. `compose.yaml` mounts `./keys.yaml` (or `STATELOCK_KEYS_FILE`), which must exist.
- The agent sends the key as `Authorization: Bearer <key>`. The SDK takes `api_key=` or the `STATELOCK_API_KEY` environment variable.
- `GET /violations/<session_id>` needs the same key and returns only that agent's sessions (401 without a valid key, 404 for other agents' sessions).
- Upload commands run on the agent's authenticated connection.
- `docker/dev-keys.yaml` holds public development keys (`slk_dev_<agent_id>`, `slk_dev_dev_reviewer` for the reviewer, `slk_dev_dev_auditor` for the auditor) for the demo agents in `compose.dev.yaml`. Never deploy it.
- `STATELOCK_AUTH_MODE=none` turns authentication off (agent IDs are self-asserted; anyone may review, named by `x-statelock-reviewer-id`) and logs a warning. Use it only for local development.
- Plugins can protect their own routes with the FastAPI dependencies `statelock.app.authenticated_identity` (agents), `statelock.app.authenticated_reviewer` (reviewers) and `statelock.app.authenticated_auditor` (auditors).

## Connecting an agent

For async Playwright, `connect_playwright()` is the whole integration:

```python
import statelock.client

statelock.client.install()                                        # Playwright's file APIs and page.request go through Statelock
async with await statelock.client.connect_playwright(playwright) as governed:
    async with governed.guard():
        page = governed.page                                      # continue with existing Playwright code
```

`connect_playwright()` reads `STATELOCK_URL` and `STATELOCK_API_KEY`, creates a single-use session URL, connects Chromium over CDP, and returns the browser plus the session metadata; `async with` closes the connection at the end. Wrap the action section in `async with governed.guard():` so a blocked action raises `StatelockPolicyViolationError` with the rule and reason instead of looking like a closed browser:

```python
from statelock.client import StatelockPolicyViolationError

try:
    async with governed.guard():
        await page.click("text=Mark as Paid")
except StatelockPolicyViolationError as violation:
    print(violation.rule, violation.reason)
```

The guard looks the violation up with the key the session was created with (else `STATELOCK_API_KEY`). If Statelock refuses that key or cannot be reached, it raises `SessionUrlError` (with `status`) instead of reporting no violation.

The sync API has the same names ending in `_sync` / `Sync`:

```python
import statelock.client

statelock.client.install_sync()
with statelock.client.connect_playwright_sync(playwright) as governed, governed.guard():
    page = governed.page
```

Under the hood, Statelock uses a CDP browser URL. Any framework that takes such a URL (Playwright, Puppeteer, browser-use, Stagehand, LangChain or CrewAI browser tools) can use one.

### Session URLs

The agent's key asks Statelock for a single-use URL:

```bash
export STATELOCK_URL=http://localhost:8010 STATELOCK_API_KEY=<the agent key>
statelock session-url        # prints http://localhost:8010/sessions/slt_... (single use, expires in 300 s)
```

```python
from statelock.client import create_session_url

session = create_session_url()                                   # STATELOCK_URL, STATELOCK_API_KEY
browser = await playwright.chromium.connect_over_cdp(session.cdp_url)
async with session.guard():                                      # session.violation() after the fact
    ...
# Stagehand: StagehandConfig(env="LOCAL", local_browser_launch_options={"cdp_url": session.cdp_url})
```

- **Two forms:** `cdp_url` is an http URL; frameworks read the WebSocket address from its `/json/version`. `ws_url` is the WebSocket itself.
- **The token:** it identifies the agent and the session. It is used up when the browser connects and expires after `STATELOCK_SESSION_URL_TTL`.
- **Storage and logs:** Statelock stores only the token's hash and redacts tokens from its logs.
- **Errors:** a request Statelock refuses raises `SessionUrlError`, with the HTTP `status`.
- **Headers instead:** agents that can send headers may connect to `ws://.../statelock` with `Authorization: Bearer <key>`; `connect_statelock(playwright, url, agent_id)` does this.

### Saved sessions

An agent can reuse a site login without holding the cookies. A session URL names a saved session: Statelock puts its cookies and localStorage into the browser before the agent connects, and, with `save_session`, saves them again when the session ends cleanly. A session that ends on a violation or an error is not saved, so the state after a blocked action is never kept.

```python
session = create_session_url(saved_session="bank-login", save_session=True)   # first run: logs in, saved at the end
session = create_session_url(saved_session="bank-login")                      # later runs: already logged in
```

```bash
statelock session-url --saved-session bank-login --save
statelock saved-sessions list            # or: statelock saved-sessions delete bank-login
```

- **Off until configured.** Set `STATELOCK_SAVED_SESSIONS_KEY_FILE` to a path outside the saved-session directory, such as a mounted secret. Statelock creates the 32-byte key there (mode 0600) if the file does not exist. Back it up: without it the saved sessions cannot be read. Without the setting, a request that names a saved session is refused (422).
- **Encryption.** Each file is encrypted with AES-256-GCM (`cryptography`). The tenant, agent and name are bound to the file, so a file copied to another agent's name does not decrypt.
- **Scope.** Saved sessions belong to one tenant and agent. Names are 1-80 characters: letters, digits, `.`, `_` and `-`. They are stored under `STATELOCK_SAVED_SESSIONS_DIR` (default: `STATELOCK_ARTIFACT_DIR/saved-sessions`).
- **What is kept.** Cookies (HttpOnly included) and the localStorage of each open page's origin. localStorage is restored through a hidden page at the origin that Statelock answers itself, so nothing reaches the site. Not kept: sessionStorage (per tab, as after a browser restart), IndexedDB, and storage of cross-origin iframes.
- **API.** `GET /saved-sessions` lists the calling agent's saved sessions and `DELETE /saved-sessions/<name>` deletes one. The client has `list_saved_sessions()` and `delete_saved_session(name)`.

### Credentials

The agent never needs the password. It types a placeholder, and Statelock types the secret:

```python
await page.fill("#password", "{{secret:bank_password}}")   # the site receives the real password
```

```yaml
# STATELOCK_SECRETS_FILE
secrets:
  - name: bank_password
    agents: [finance_reconciliation_agent]       # who may use it
    url_contains: https://bank.example.com/login # the origin, then part of the path
    value_env: BANK_PASSWORD                     # or value_file: /run/secrets/bank_password
    # password_fields_only: true                 # default: only into password fields
```

- **Where it is typed.** Statelock replaces the placeholder in typed text (`Input.insertText`, which Playwright's `fill` sends) after the action passes its pre-conditions. It does so only for the listed agents, on a page whose URL has exactly the origin in `url_contains` (`http(s)://host[:port]`) and a path containing its path, and (by default) only into a password field. Anywhere else, the placeholder ends the session (`secret_injection`).
- **What the agent sees.** The placeholder. Statelock replaces the value with `[SECRET]` in every answer the browser sends the agent and in every `request_access` response. Reading the field back returns `[SECRET]`.
- **Evidence.** The typed text is recorded as `[SECRET]`, with the secret's name (`statelock_secrets`). The value is removed from action records and the network log.
- **Values.** They are read from the proxy's environment or files when it starts, and a missing value fails at startup. The agent's own environment never holds them.
- **Limits.** Typing the placeholder key by key (`keyboard.type`) sends it literally. Page code the agent runs could read the field and transform the value before returning it; no filter can recognise that.
- **With a saved session**, the login usually happens once: later sessions start logged in.

Cookies are not exported either. Playwright's `page.request` / `context.request` (without `install()`), and cookies copied into another HTTP client, would act as the logged-in user outside Statelock. Reading cookies over CDP returns only what the agent's policies allow (`cookie_access`). With no `cookie_access`, the read is declined (`cookie_export`), so those calls fail instead of bypassing Statelock, and the session continues. Either way the read is recorded. For HTTP, use `request_access`: after `install()`, `page.request` goes through Statelock.

What no SDK can stop: plain HTTP from the agent's own process (for example `requests` with a password it holds). Use credential injection so the agent holds no password, and deploy the agent so it can reach only Statelock.

### Files

`statelock.client.install()` (async) or `statelock.client.install_sync()` (sync), once at startup, routes Playwright's own file APIs through Statelock:

- **Uploads:** `set_input_files` (on a page, locator or element handle) and `FileChooser.set_files` upload the files through Statelock. The upload is governed, and the file's name, size and SHA-256 are recorded.
- **Downloads:** `page.expect_download()` waits for the download Statelock checked. A download is checked when it begins (the page's policies, including `restrict_downloads`) and when it completes (size, SHA-256). Its value works like Playwright's Download (`save_as`, `path`, `suggested_filename`, `url`).
- **Other browsers are untouched:** each browser is asked once whether it is a Statelock session (`Statelock.session`).
- **Without `install()`:** use `conn.set_input_files(page, target, files)` and `conn.expect_download(page)` from a `StatelockConnection`.

Limits (see the [roadmap](https://github.com/inferlink/statelock/blob/main/ROADMAP.md)):

- `page.on("download")` listeners are not covered, and `install_sync()` does not cover sync `page.request` yet.
- File inputs inside iframes are not supported yet (main frame only).
- Upload rules (`restrict_uploads`) check name, extension, size and count, not file contents.
- File contents are not stored with the evidence. Only name, size and SHA-256 are recorded, and uploaded files are deleted when the session ends.

### Frameworks

**Script clicks.** Frameworks that click with page scripts (`element.click()`, as Stagehand does) work without changes. Statelock scrolls the element into view and replays the click as real mouse input at the element, checked and recorded like any click. It is replayed only when the element is visible and not covered, in the top frame; otherwise the click is blocked. Synthetic events from the site's own code, and other synthetic events (submit, drop, paste), are still blocked.

**browser-use.** browser-use takes a CDP URL through `BrowserSession` and drives the browser over raw CDP, so every click and key press goes through Statelock (`pip install "statelock-ai[browser-use]"`, tested with browser-use 0.13, Python 3.11+):

```python
from browser_use import Agent
from statelock.integrations.browser_use import create_browser_use_session

governed = create_browser_use_session()   # STATELOCK_URL, STATELOCK_API_KEY
agent = Agent(task="...", browser_session=governed.browser, llm=...)
await agent.run()
governed.raise_if_violation()             # StatelockPolicyViolationError (rule, reason)
```

browser-use reports a blocked action to its model as a failed step, not as an exception, so the run continues; Statelock has already ended the session, so nothing else gets through. `governed.violation()` / `raise_if_violation()` give the rule and reason afterwards. A lookup Statelock refuses (a wrong key) raises `SessionUrlError`, not "no violation". To build the browser-use object yourself, pass `create_session_url().cdp_url` to `BrowserSession(cdp_url=...)`. browser-use sends anonymous telemetry by default: set `ANONYMIZED_TELEMETRY=false` to turn it off.

**LangChain and CrewAI.** Give LangChain's Playwright toolkit a browser from a session URL, and wrap its tools with `governed_playwright_tools` (`pip install "statelock-ai[langchain]"`):

```python
from statelock.client import create_session_url
from statelock.integrations.langchain import crewai_tools, governed_playwright_tools

session = create_session_url()
browser = await playwright.chromium.connect_over_cdp(session.cdp_url)
tools = governed_playwright_tools(browser, session)
# CrewAI: use sync Playwright, then tools = crewai_tools(governed_playwright_tools(browser, session))
```

Every click and keystroke is governed without the wrapper. What the wrapper adds:

- **The agent learns why it stopped.** Without it, a blocked action looks like "browser closed". With it, when a tool fails or the browser disconnects, the wrapper asks Statelock, and a violation raises `StatelockPolicyViolationError` (rule, reason), which stops the agent run. Successful calls cost no lookup.
- **Timeout.** It raises the click tool's 1 s timeout to 10 s, because a governed click waits for Statelock's checks.
- **Selector bug.** It turns off the tool's `visible_only` suffix. Current Playwright never matches it, so every click would time out.

The wrapper returns copies; the tools passed in are not modified. `statelock_tools(tools, session)` wraps other LangChain tools that drive the governed browser. CrewAI's `StagehandTool` only runs on Browserbase's cloud browsers, so it cannot use a Statelock session; use the LangChain toolkit through CrewAI instead.

**Stagehand.** `examples/ojs/` is a Stagehand agent on a mock journal: three Statelock lines in `main()`, and a password placeholder instead of the password (`pip install "statelock-ai[stagehand]"`).

**JavaScript and TypeScript.** `@statelock/client` (`js/`) is not yet on npm; build it from `js/` (`npm ci` runs the build). It has:

- `createSessionUrl()`, `session.guard()` and `secret()`;
- `install(browser)` for Playwright's file APIs;
- CDP helpers for other frameworks.

`examples/ojs-ts/` is the OJS agent in TypeScript on Stagehand v3. Stagehand v4 is not supported: its driver runs as a browser extension, not over CDP.

## Configuration

All settings are `STATELOCK_*` environment variables (`statelock/settings.py`):

| Variable | Default | Purpose |
|---|---|---|
| `STATELOCK_POLICY_FILE` | `/app/policies/default.yaml` | Policy bundle. A missing file fails at startup. |
| `STATELOCK_EXTRA_POLICY_FILES` | none | More policy files, comma-separated, loaded with `STATELOCK_POLICY_FILE` as one bundle (ids unique across them). The dev stack adds `examples/ojs/ojs_policy.yaml`. |
| `STATELOCK_RULE_MODULES` | none | Custom rule modules, comma-separated: module names or `.py` paths, loaded before the policy files (see Custom rules). |
| `STATELOCK_ARTIFACT_DIR` | `/app/artifacts` | Local artifact root. |
| `STATELOCK_PLUGINS` | `auto` | `auto`, `none`, or a comma-separated list of plugin names. |
| `STATELOCK_DEBUG` | `false` | Also writes the latest action record to `artifacts/latest.json`. |
| `STATELOCK_DEMO` | `false` | Enables the demo pages `/demo/finance`, `/demo/bank` and `/demo/erp`. |
| `STATELOCK_LOG_LEVEL` | `INFO` | Level for Statelock's logging setup. Empty leaves logging to the host. |
| `STATELOCK_AUTH_MODE` | `api_key` | `api_key` or `none` (development only). |
| `STATELOCK_AUTH_KEYS_FILE` | none | Keys file (hashes). Required when `STATELOCK_AUTH_MODE=api_key`. |
| `STATELOCK_SESSION_URL_TTL` | 300 | Seconds a session URL (`POST /sessions`, `statelock session-url`) stays usable. |
| `STATELOCK_SECRETS_FILE` | none | Secrets agents type as `{{secret:name}}` (credential injection). Values come from `value_env` or `value_file`; a missing one fails at startup. |
| `STATELOCK_SAVED_SESSIONS_KEY_FILE` | none (saved sessions off) | AES-256-GCM key for saved browser sessions, outside the saved-session directory. Created if missing. |
| `STATELOCK_SAVED_SESSIONS_DIR` | `STATELOCK_ARTIFACT_DIR/saved-sessions` | Where saved browser sessions are stored. |
| `STATELOCK_UPLOAD_DIR` | system temp | Base folder for per-session upload folders (deleted when the session ends). It must be readable by Chromium. |
| `STATELOCK_UPLOAD_MAX_FILE_BYTES`, `STATELOCK_UPLOAD_MAX_SESSION_BYTES` | 100 MiB, 500 MiB | Upload limits. |
| `STATELOCK_DOWNLOAD_DIR` | system temp | Base folder for per-session download folders (deleted when the session ends). |
| `STATELOCK_DOWNLOAD_MAX_FILE_BYTES`, `STATELOCK_DOWNLOAD_MAX_SESSION_BYTES` | 100 MiB, 500 MiB | Download limits; a larger download is cancelled (`download_limit`). |
| `STATELOCK_PERCEPTION_MODEL` | none (visual checks fail closed) | litellm model for `visual_assert`, e.g. `gemini/gemini-3.5-flash`, `ollama_chat/qwen2.5vl:7b` or `hosted_vllm/Qwen/Qwen2.5-VL-7B-Instruct`. Needs `statelock-ai[vlm]`. |
| `STATELOCK_PERCEPTION_API_BASE`, `STATELOCK_PERCEPTION_API_KEY` | none | The model server's URL and key (a local vLLM or Ollama server needs only the URL). |
| `STATELOCK_PERCEPTION_TIMEOUT`, `STATELOCK_PERCEPTION_RETRIES` | 20, 1 | Seconds per model call; extra attempts after invalid output or an error. |
| `STATELOCK_PERCEPTION_LOCAL_ONLY` | `false` | Refuse to start unless the model server (`..._API_BASE`) is on this machine or a private network. |
| `STATELOCK_REVIEW_TIMEOUT` | 300 | Seconds a paused action waits for a reviewer before it is blocked. |
| `STATELOCK_REVIEW_HISTORY_SIZE` | 1000 | Decided reviews kept in memory for the review API. |
| `STATELOCK_VIOLATION_REGISTRY_SIZE` | 1000 | Violations kept in memory for `GET /violations/<session_id>`. |
| `STATELOCK_REPLAY_SCRIPT_CLICKS` | `true` | Replay a plain `element.click()` from agent code as a governed real click. `false`: such clicks are violations. |
| `STATELOCK_AGENT_SCRIPT_GRACE` | 0.5 | Seconds after agent code returns that a click it started still counts as the agent's (and is replayed). |
| `STATELOCK_REPLAY_WAIT` | 10 | Seconds the agent's next command waits, at most, for a replayed click to finish. |
| `STATELOCK_CHROMIUM_HOST` | `127.0.0.1` | Address Chromium's remote debugging port listens on. |
| `STATELOCK_CHROMIUM_SANDBOX` | `false` (`1` in `compose.yaml`) | Chromium sandbox. It needs a non-root user and a seccomp profile that allows user namespaces; `compose.yaml` provides both (see [docker/README-sandbox.md](https://github.com/inferlink/statelock/blob/main/docker/README-sandbox.md)). If the sandbox cannot start, sessions are refused. |
| `STATELOCK_CAPTURE_TIMEOUT` | 2.5 | Seconds a state capture may take before the action is blocked. |
| `STATELOCK_POST_CAPTURE_SETTLE` | 0.2 | Seconds to wait after a commit action before capturing the page for post-conditions. |
| `STATELOCK_HELD_RESPONSE_TIMEOUT` | 10 | Seconds to wait for the browser's response to a held commit action. |
| `STATELOCK_VIOLATION_CLOSE_DELAY` | 0.5 | Seconds between the in-band violation error and closing the session. |
| `STATELOCK_GUARD_READY_TIMEOUT` | 10 | Seconds an agent command on a new page or iframe waits for Statelock's setup there (the page guard). |
| `STATELOCK_GUARD_COMMAND_TIMEOUT` | 5 | Seconds Statelock's own CDP commands to the browser may take. |
| `STATELOCK_ATTRIBUTION_TIMEOUT` | 1 | Seconds to wait for a request's initiator before it is logged as `unknown`. |
| `STATELOCK_TRUSTED_SUBMIT_WINDOW` | 3 | Seconds after a submit from real input during which the form's navigation counts as trusted. |
| `STATELOCK_AGENT_NAVIGATION_WINDOW` | 10 | Seconds after an agent navigation command during which a page load is attributed to it. |
| `STATELOCK_MEMORY_SETTLE` | 0.5 | Seconds after a page load before the second capture of remembered fields. |

## Extending

- **Rules.** Subclass `statelock.policy.rules.Rule`, decorate it with `@register_rule`, and load it with a `statelock.rules` entry point or `STATELOCK_RULE_MODULES` (see Custom rules).
- **Storage.** Implement `statelock.audit.sink.ArtifactSink`. It receives versioned `ActionRecord`s (schema `"4"`, `statelock.audit.records`; the schema history is in that module) in sequence order.
- **Plugins.** Expose an object with `name` and `setup(ctx)` in the `statelock.plugins` entry point group. `setup` can:
  - replace or wrap `ctx.services.sink`;
  - subscribe to `ctx.services.events` (`violation`, `action_written`, `request_recorded`, `review_requested`, `review_decided`, `session_closed`);
  - add routes to `ctx.app`, which reach services with `statelock.app.get_services(request.app)`.
- **Perception.** `visual_assert` uses `LiteLLMPerceptionEvaluator` when `STATELOCK_PERCEPTION_MODEL` is set. For another backend, implement `statelock.policy.perception.PerceptionEvaluator` and set it on `get_services(app).evaluator.perception` in a plugin.

## Statelock Enclave

Statelock Enclave is InferLink's commercial add-on for production and audit, built on the extension points above. It adds:

- tamper-evident evidence storage (hash chains, signed anchors, S3 Object Lock);
- a witness run by InferLink that countersigns each session's anchors in an append-only log, so an auditor who trusts neither you nor InferLink can check the evidence with `statelock-verify`, a verifier InferLink provides to auditors;
- a forensic replay dashboard;
- deployment inside your own cloud.

## Repository layout

```text
src/statelock/
  core/        shared types: actions, state, verdicts, enums
  policy/      policy schema, fields and remembered values, rule registry, evaluator, perception (VLM)
  audit/       record schema, redaction, sequenced writer, ArtifactSink, LocalJsonSink
  proxy/       bridge (session), governor, memory watcher, reporter, targets, inspector,
               guard, attribution, commands, uploads, downloads, files, tasks, connection, pages, worlds, js/
  client/      SDK: session URLs, connect_playwright / connect_playwright_sync, install() / install_sync() for
               Playwright's file APIs (async files.py + install.py, sync.py, shared protocol in transfer.py),
               connection, violations
  integrations/  langchain.py (LangChain / CrewAI browser tools), browser_use.py (browser-use)
  review/      human review: queue of paused actions, review API, /review page
  demo/        /demo/finance, /demo/bank, /demo/erp (STATELOCK_DEMO=1 only)
  app.py       create_app() factory, get_services(), authenticated_identity, authenticated_reviewer
  sessions.py, tokens.py   session URLs (POST /sessions, single-use tokens), /saved-sessions
  saved_sessions.py        encrypted saved browser sessions (AES-GCM store, restore, save)
  credentials.py           credential injection: secrets file, {{secret:name}} placeholders, scrubbing
  auth.py      agent, reviewer and auditor API keys
  __main__.py  the statelock command: the proxy (no subcommand), keygen, hash-key, session-url,
               saved-sessions, check-sandbox, perception-check
  settings.py, plugins.py, events.py, services.py, registry.py, wire.py
tests/         unit tests; browser tests (test_browser_*.py, skipped without Chromium)
examples/      rogue_agent.py, finance_agent.py, portal_agent.py; ojs/ (Stagehand OJS agent, mock OJS site, its policy and custom rule);
               ojs-ts/ (the OJS agent in TypeScript on Stagehand v3)
js/            @statelock/client: the JavaScript/TypeScript SDK (session URLs, guard, Playwright install())
policies/      default.yaml
docker/        Dockerfile (targets: runtime, dev, ojs), seccomp profile, dev keys, dev secrets
```

## Development

The Docker Quick start uses `compose.dev.yaml`, which mounts public development keys and enables demo pages. To run `compose.yaml` alone, first install the `statelock-ai` CLI, configure a policy for your agent, and create its keys file:

```bash
statelock keygen --agent-id my_agent --append keys.yaml
docker compose -f compose.yaml up -d --build
```

To run the test suite in Docker, use the development override:

```bash
docker compose -f compose.yaml -f compose.dev.yaml --profile test run --rm statelock-tests
```

Other demo services and profiles are listed in [compose.dev.yaml](compose.dev.yaml); the [OJS walkthrough](examples/ojs/README.md) covers the journal agent.

For a local development install:

```bash
pip install -e ".[dev]"
python -m playwright install chromium
ruff check src tests examples && ruff format --check src tests examples && python -m mypy && python -m pytest -q
```

- Python 3.10+ (the `browser-use` extra needs 3.11+). The Docker images use Python 3.12 (Playwright's Ubuntu 24.04 "noble" image). `mypy` is strict for `core` and `policy`.
- The Docker image pins Playwright 1.56.0 (Chromium 141) and runs as `pwuser`. On Linux hosts, `./artifacts` must be writable by that user.
- Injected JavaScript lives in `src/statelock/proxy/js/`.
- Contributions: see [CONTRIBUTING.md](https://github.com/inferlink/statelock/blob/main/CONTRIBUTING.md). Every commit needs a DCO sign-off (`git commit -s`).
- Security issues: see [SECURITY.md](https://github.com/inferlink/statelock/blob/main/SECURITY.md).

## Roadmap

See [ROADMAP.md](https://github.com/inferlink/statelock/blob/main/ROADMAP.md).

## License

Apache License 2.0. See [LICENSE](https://github.com/inferlink/statelock/blob/main/LICENSE) and [NOTICE](https://github.com/inferlink/statelock/blob/main/NOTICE).
