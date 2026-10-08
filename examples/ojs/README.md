# OJS screening agent through Statelock

This is a Stagehand agent for Open Journal Systems (OJS 3.x). It logs in as an editor (without holding the password), lists the active submissions, and screens each one:

- downloads the manuscript;
- reads the comments to the editor;
- reads the contributors;
- runs basic checks.

Afterwards it declines the papers that a decisions file marks for rejection. Every click, key press and download goes through Statelock, which checks it against the policy in `ojs_policy.yaml` and records it as evidence.

The agent is ordinary Stagehand and Playwright code. The only Statelock code is three lines in `open_session()` and `run_governed()`, and a placeholder where the password would be:

```python
# open_session()
session = create_session_url(saved_session="ojs-editor", save_session=True)   # a one-time browser URL, logged in if a run saved the login
# run_governed()
statelock.client.install()          # Playwright's own uploads and downloads go through Statelock
stagehand = Stagehand(StagehandConfig(env="LOCAL", local_browser_launch_options={"cdp_url": session.cdp_url}, ...))
async with session.guard():         # optional: a violation raises StatelockPolicyViolationError
    ...
await password_field.fill("{{secret:ojs_password}}")   # Statelock types the real password
```

Journal-specific checks (for example comparing the PDF with the journal's template, or matching authors) and the connection to where the editors record their decisions are left out. Add them in `basic_checks` and `OjsAgent.decisions`.

## What Statelock handles without code changes

| Agent code | What happens through Statelock |
|---|---|
| Stagehand's `act()` clicks, which use `el.click()` in the page | Replayed as real mouse clicks at the element, checked and recorded. |
| `page.expect_download()` and `download.save_as()` | After `install()`, the download is checked (`restrict_downloads`) and its SHA-256 recorded, then it arrives as usual. |
| `page.request.get(...)` with the browser's cookies | After `install()`, Statelock makes the request in the page only if the policy allows it (`request_access`). `ojs_policy.yaml` has no `request_access`, so it is declined with `StatelockRequestError`, and it cannot bypass Statelock. Download through the page instead. |
| Filling the password field with `{{secret:ojs_password}}` | Statelock types the password from its secrets file (only for this agent, only on the login page, only into a password field). The agent never has it; reading the field back returns `[SECRET]`, and the evidence records `[SECRET]` and the secret's name. |
| Later runs | The saved session `ojs-editor` restores the login, so the agent skips the login form. |

Two choices in this agent are good practice rather than Statelock requirements:

- **The password:** even the placeholder is filled directly instead of being written into an LLM prompt.
- **The rejection email:** it is typed into the editor instead of injected with `innerHTML`, so the email sent to the authors is in the evidence.

## Run it against the mock journal

`mock_ojs.py` is a small stand-in for an OJS editor site. Use it instead of a real journal. The agent refuses a host other than localhost unless `OJS_ALLOW_REMOTE=1`.

The development setup already has this agent:

- `compose.dev.yaml` loads `ojs_policy.yaml` next to the default policy (`STATELOCK_EXTRA_POLICY_FILES`) and its custom rule `ojs_rules.py` (`STATELOCK_RULE_MODULES`);
- `docker/dev-keys.yaml` has the key `slk_dev_ojs_screening_agent`;
- `docker/dev-secrets.yaml` gives it the secret `ojs_password` on `/index.php/journal/login`. `compose.dev.yaml` sets its value, the mock's public password `mock-editor-pass`, in the proxy's environment (`OJS_PASSWORD`); the agent's environment has no password;
- `compose.dev.yaml` sets a saved-session key, so the login is kept between runs.

To start over, delete the saved login: `STATELOCK_API_KEY=slk_dev_ojs_screening_agent statelock saved-sessions delete ojs-editor`. Set `OJS_SAVED_SESSION=` to log in on every run.

### With Docker (no local install)

`compose.dev.yaml` runs the mock journal (`ojs-mock`) and the agent in containers next to the proxy. Create `.env` first (`cp .env.example .env`; `compose.yaml` reads it). The commands below assume `COMPOSE_FILE=compose.yaml:compose.dev.yaml` in `.env` (see `.env.example`); otherwise add `-f compose.yaml -f compose.dev.yaml` to each. Each command starts what it needs:

```bash
docker compose up -d --build                      # Statelock on localhost:8010
docker compose --profile ojs-agent run --rm ojs-agent                 # dry run: screens 101-103, fills and cancels the decline of 102
docker compose --profile ojs-record-agent run --rm ojs-record-agent          # records the decline of 102 (in the mock)
docker compose --profile ojs-block-agent run --rm ojs-block-agent           # clicks "Send to Review": blocked, the session ends (exit 2)
docker compose --profile ojs-agent stop ojs-mock  # stop the mock journal
```

- **Image.** The `ojs` target of `docker/Dockerfile` (dev + Stagehand).
- **Decisions.** `demo_decisions.json` declines paper 102.
- **Output.** Results and manuscripts go to `./ojs-output`. On Linux hosts it must be writable by the image's `pwuser`, like `./artifacts`.
- **Login.** The first run logs in with the placeholder, and later runs reuse the saved session. The mock keeps its state (declined papers) until it restarts.
- **Enclave.** With Statelock Enclave, this evidence is also hash-chained and witnessed.

### Without Docker for the agent

```bash
pip install -e ".[stagehand]"
docker compose up -d                                        # Statelock on localhost:8010 (compose.yaml + compose.dev.yaml)
docker compose --profile ojs-agent up -d ojs-mock             # the mock journal on the Compose network
export STATELOCK_URL=http://localhost:8010 STATELOCK_API_KEY=slk_dev_ojs_screening_agent
export OJS_BASE_URL=http://ojs-mock:8081/index.php/journal    # as the proxy's browser sees it
export OJS_USERNAME=editor                                   # no password: Statelock types it
export OJS_DECISIONS_FILE=examples/ojs/demo_decisions.json    # the file the proxy's ojs_decision rule checks too
python examples/ojs/ojs_agent.py                             # dry run
OJS_RECORD_DECISIONS=1 python examples/ojs/ojs_agent.py      # records the decline
```

The agent and the proxy's `ojs_decision` rule read the same decisions: a decline the proxy's copy does not have, or with another email, is blocked.

The page URL must work for Chromium, which runs in the Statelock container. The development secret allows login only at `http://ojs-mock:8081/index.php/journal/login`, so the mock uses that Compose hostname. To run the mock on the host instead, change `url_contains` in `docker/dev-secrets.yaml` to `http://host.docker.internal:8081/index.php/journal/login`, restart the proxy, and use that host in `OJS_BASE_URL`. On Linux, Docker defines `host.docker.internal` only when asked: add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `statelock` service first. The mock must listen on an address the container can reach (`MOCK_OJS_HOST=0.0.0.0`). For a real journal, configure its URL and secret scope explicitly.

Results go to `ojs-output/results.json`, with the manuscripts beside it. The evidence is in the Statelock artifacts for the session.

Stagehand's LLM (`observe()`) is used only when a fixed OJS selector misses. Enable it with `STAGEHAND_MODEL` and `MODEL_API_KEY`. Without them the agent uses the selectors alone, which is how the tests run.

## The policy

`ojs_policy.yaml` enforces the browser checks a screening agent would make about itself, so a misled or buggy agent cannot skip them. It has four policies:

| Agent step | Policy | Before the action | After the action |
|---|---|---|---|
| Any action on the journal | `ojs-journal` | Only declining: Send to Review, Accept and Skip Review, Send to Production, Delete, Remove, Unassign, Revert Decline and similar buttons are blocked (the session ends). Downloads within a size limit and count (a `.tex` or `.docx` manuscript must be downloaded so the PDF check can fail it). No uploads. | |
| Login page | `ojs-login` | The username field, password field and login button are there. | |
| Click Login (or Enter) | `ojs-login` | The username is `editor`; a password was entered (only its length is read). | No login form, no "Invalid username or password", the Submissions menu. |
| Submissions list | `ojs-submissions` | The Submissions page with its All Active tab. | After "All Active": the active list is shown. |
| Any action on a paper page | `ojs-workflow` | The paper id on the page equals the one in the URL (the agent's URL guard). | |
| Click Decline Submission | `ojs-workflow` | The files are shown, the button is there, and the decision source says to decline this paper (`ojs_decision`). | The decline form opened, with its email editor. |
| Click Record Editorial Decision | `ojs-workflow` | The Record button is shown, the email editor holds at least 50 characters (the "Regarding submission N:" line plus the agent's 20-character minimum), and the email is the one the decision source has (`ojs_decision`). | The form closed, the Decline button is gone, the paper shows "Declined". |

`ojs_decision` is a custom rule (`ojs_rules.py`; see Custom rules in the main README). It reads `demo_decisions.json` at the moment of the click; for a real journal, replace `read_decision` with a lookup in a spreadsheet or service holding the editors' decisions, so each decline is re-checked at the moment of the click. If the rule fails or does not answer within 10 s, the click is blocked.

To make each recorded rejection wait for an editor, add this rule:

```yaml
      - prohibit_click_text: {values: [Record Editorial Decision], on_fail: review}
```

Each rejection then pauses until a reviewer approves or denies it at `/review`. Raise the agent's action timeout above `STATELOCK_REVIEW_TIMEOUT`: `OJS_ACTION_TIMEOUT` (seconds, default 360).

## Tests

`tests/test_example_ojs.py` runs the agent against the mock journal through a real proxy and Chromium. It is skipped when Stagehand is not installed. It covers:

- screening all three papers, including a `.tex` manuscript that is downloaded and flagged;
- recording a decline, with the typed email in the evidence;
- the password typed by Statelock for the placeholder, and absent from the evidence;
- a second run that reuses the saved login;
- a Stagehand `act()` click replayed as a governed click;
- a prohibited workflow button, chosen through `act()`, ending the session;
- each check in the table above, broken on purpose (the wrong account, a failed login, a list that does not open, a page showing another paper, a withdrawn decision, another email, a short email the agent's own check let through, a decline the journal did not record, an unreadable decision source), each blocked by the named policy and rule.
