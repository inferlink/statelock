# Tutorial: draft and test the OJS policy with Policy Studio

This tutorial writes a policy for the OJS screening agent without writing YAML
by hand, then proves it on the agent. You will:

1. record, in Chrome, a decline the policy must allow and a wrong step it must
   block, on the mock journal;
2. describe in [Policy Studio](../../tools/policy-studio/README.md) what must be
   true at each step, and have a model draft the policy;
3. start Statelock with the draft and run the real agent twice: the decline
   must pass, and the wrong step must be blocked.

Everything runs locally in Docker, against the mock journal. The agent is not
changed. You need Docker, Chrome, and an API key for a model LiteLLM supports
(the default is Gemini). See Policy Studio's README for what is sent to the
model and its limits.

## 1. Set up

From the repository root, create `.env` if you have not (`cp .env.example .env`)
and turn on the development setup in it (the demo agents and Policy Studio):

```bash
COMPOSE_FILE=compose.yaml:compose.dev.yaml
```

**Model key.** Policy Studio asks a model to draft the policy, so it needs an
API key from a model provider. Statelock itself does not use it. The default
model is Google's Gemini: create a free key at
[Google AI Studio](https://aistudio.google.com/apikey) (**Create API key**).
Export it in the terminal you run Docker Compose from:

```bash
export STATELOCK_POLICY_STUDIO_API_KEY=your-gemini-api-key
```

Keep it out of `.env`: `compose.yaml` passes `.env` to the proxy container too.
To use another provider, set the model as well, as a
[LiteLLM model name](https://docs.litellm.ai/docs/providers), with that
provider's key:

| Provider | `STATELOCK_POLICY_STUDIO_MODEL` | `STATELOCK_POLICY_STUDIO_API_KEY` |
| --- | --- | --- |
| Google Gemini (default) | unset | a Google AI Studio key |
| OpenAI | `openai/<model>` | an OpenAI API key |
| Anthropic | `anthropic/<model>` | an Anthropic API key |
| Ollama on your machine | `ollama_chat/<model>`, and `STATELOCK_POLICY_STUDIO_API_BASE=http://host.docker.internal:11434` | none |

A small local model may draft poorly; a strong hosted model works best here.

Build the images and start Statelock (with the hand-written OJS policy for now):

```bash
docker compose --profile ojs-agent build
docker compose up -d --build
```

## 2. Record the two runs

Start a mock journal for Chrome on your machine, on port 8081. It runs in the
foreground; `Ctrl`+`C` stops it, and starting it again resets it.

```bash
docker compose --profile ojs-agent run --rm -p 127.0.0.1:8081:8081 ojs-mock
```

Open http://127.0.0.1:8081/index.php/journal/login in Chrome and log in with
the mock's public demo credentials, `editor` / `mock-editor-pass`. Log in
**before** recording: the login is not part of this policy.

Open DevTools (`Command`+`Option`+`I` on macOS, `Ctrl`+`Shift`+`I` elsewhere).
Click **⋮** at its top right, then **More tools** → **Recorder**.

**Run 1, `OJS decline 102` (must pass).** Create a recording with this name and
start it. Then:

1. Open **Submissions**, select **All Active**, and open submission **102**,
   the paper `demo_decisions.json` says to decline.
2. Click **Decline Submission**. In the email editor, enter the email for 102
   from `demo_decisions.json`, then click **Record Editorial Decision**.
3. When the paper shows as declined, stop the recording and export it as
   **JSON**.

**Run 2, `OJS send to review` (must be blocked).** A screening agent may only
decline the papers the editors decided to decline. **Send to Review** moves a
paper on to peer review: an editor's decision, not the agent's. An agent
misled by a page, or by its own model, could click it anyway; this is the step
the demo's rogue agent takes, and the policy must stop it. Restart the mock (`Ctrl`+`C`, run it again), log in again,
create a recording with this name and start it. Open **Submissions**, **All
Active**, submission **101**, click **Send to Review**, stop the recording and
export it.

Chrome performs these actions for real; that is why this uses the mock. The
exported files hold everything you typed: keep them out of the repository.
Stop the mock when done.

## 3. Describe the checks

Start Policy Studio and open http://127.0.0.1:8765/:

```bash
docker compose --profile policy-studio up --build policy-studio
```

It loads the example's custom rule `ojs_decision` (`ojs_rules.py`), so the
model can use it.

1. **Load recording** (top bar): `OJS decline 102`. The first recording is a
   run that must pass.
2. Select a step in the timeline, choose **Before action** or **After action**,
   write what must be true, and click **Add check**. The recording shows what
   was done; the checks say why it was right. Use your own words; for example:

| Step | Before it happens | After it happens |
| --- | --- | --- |
| Decline Submission | The page shows the submission the URL names, with its files; the editorial decision source says to decline it. | The decline form and email editor appear. |
| Record Editorial Decision | The email is the one the decision source has for this submission. | The form closes and the paper shows Declined. |

3. **Add recording** (at the top of the timeline, under the recording's name):
   `OJS send to review`. It is added as **must be blocked**. Under **Why it must
   be blocked**, write the reason ("Only declining is allowed: Send to Review is
   not a screening decision"). Select its **Send to Review** click and click
   **Must be blocked at this step**. The **Recording** list above the timeline
   switches between the two recordings.
4. **Send message** to discuss the checks. Each request opens a preview of
   exactly what will be sent; nothing goes to the model until you click
   **Send to model**. Typed values and recording titles are never sent.

Expect questions, and answer them in the chat:

- **Where do the decision and email come from?** From `demo_decisions.json`,
  which the custom rule `ojs_decision` reads.
- **Only submission 102, or every active one?** Every active submission: the
  paper ID must not be a fixed value in the policy.
- **Which site?** Tell it that the agent reaches the journal as
  `http://ojs-mock:8081`, not `127.0.0.1:8081` as recorded, so the policy must
  match the path `/index.php/journal/` only.

## 4. Draft the policy

Click **Draft YAML**. Enter `ojs_screening_agent` as the agent ID and
`/index.php/journal/` as the page scope, then review and send the request.
When the draft passes the schema check, the chat names the rule the model
expects to stop `OJS send to review`, and **Download YAML** saves it.

Compare it with the hand-written [`ojs_policy.yaml`](ojs_policy.yaml) (see
[the policy table](README.md#the-policy)): which checks it found, which
selectors it chose, and which it marked as needing manual work.

## 5. Test the draft on the agent

Save the draft as `examples/ojs/ojs_policy.draft.yaml`. Files ending in
`.draft.yaml` are ignored by git, and `examples/` is mounted into the proxy.
Statelock loads policies only at startup, so restart it with the draft in place
of `ojs_policy.yaml`. Export the variable: every command below must see it.
Without it, Docker Compose recreates Statelock with `ojs_policy.yaml` before
running the agent (you would see `Container statelock Recreated`), and the run
tests the hand-written policy instead of the draft.

```bash
export STATELOCK_OJS_POLICY_FILE=/app/examples/ojs/ojs_policy.draft.yaml
docker compose up -d --force-recreate statelock
docker compose exec statelock printenv STATELOCK_EXTRA_POLICY_FILES   # must print the draft's path
```

Then run the agent once for each recording. Each command starts its own mock
journal (the `ojs-mock` service, on the Docker network), so the one you
recorded with is not needed; stopping the mock first gives each run a fresh
journal.

```bash
# Run 2, OJS send to review: must be blocked (the block agent opens the first paper and clicks Send to Review)
docker compose --profile ojs-agent stop ojs-mock
docker compose --profile ojs-block-agent run --rm ojs-block-agent; echo "exit $?"

# Run 1, OJS decline 102: must pass (the record agent screens papers 101 to 103 and declines 102)
docker compose --profile ojs-agent stop ojs-mock
docker compose --profile ojs-record-agent run --rm ojs-record-agent; echo "exit $?"
```

What to check:

| Run | Expect | Then check |
| --- | --- | --- |
| Block agent | `exit 2`, and `Statelock stopped the agent: <rule>: <reason>`. Stagehand logs errors about a closed browser just before it: that is the session ending. | The rule is the one the model named, and the click it stopped is Send to Review. Blocked by another rule, or earlier, means the draft does not do what the model said. |
| Record agent | `exit 0` | `ojs-output/results.json` shows the decline of 102. |

Each violation's record, with the policy id, rule and values, is in
`artifacts/sessions/<session-id>/` (the Enclave dashboard shows it too, with
the step). The hand-written policy's ids are `ojs-journal`, `ojs-login`,
`ojs-submissions` and `ojs-workflow`: seeing one of them means Statelock is not
running the draft.

The record agent does more than the recording did (it opens every paper and
downloads the manuscripts), so a draft can block it on a step you never
recorded. That is the test working: tell the model in the chat what the agent
must also be allowed to do, or fix the YAML, and repeat.

### Example: a draft that blocks the run that must pass

In one run of this tutorial, the draft blocked Send to Review as asked, but
the record agent ended with `exit 2`:

```text
ERROR Statelock stopped the agent: ojs_decision: Blocked: the email in the page is not the one the decision source has for paper 102
```

The agent had typed the right email. The draft read it from the wrong place:

```yaml
      - name: email_body
        selector: "body"
        frame: "#personalMessage"
```

No element has the id `personalMessage`. The email editor is an iframe whose
id contains it (`personalMessage-102_ifr`; see `mock_ojs.py`), so the field
was empty and the email never matched. A recording names the element you
clicked, not every element a check reads, so a model can guess wrong here.
The hand-written policy uses `frame: "iframe[id*='personalMessage']"`.

Fix the selector in the YAML, or tell the model in the chat ("the email editor
is an iframe whose id contains `personalMessage`, such as
`personalMessage-102_ifr`") and download the new draft. Then restart Statelock,
which reads policies only at startup, and run both agents again:

```bash
docker compose up -d --force-recreate statelock
```

Run both again after every change: a fix for the passing run can loosen the
block.

When both runs behave, the draft does what you asked on these cases. Before
relying on it, add a recording for each other way the agent could go wrong
(`tests/test_example_ojs.py` breaks each check of the hand-written policy on
purpose: another paper's page, another email, a withdrawn decision) as a run
that must be blocked, and test again.

Put Statelock back on the hand-written policy:

```bash
unset STATELOCK_OJS_POLICY_FILE
docker compose up -d --force-recreate statelock
```
