# Policy Studio

Policy Studio is an experimental aid for writing Statelock policies. You load
Chrome Recorder exports of a task done by hand: runs the policy must allow, and
runs it must block, each with the step where it must be stopped and why. You
attach checks to their steps in your own words ("before this click, the page
shows the right paper"), discuss them with a model, and get a draft policy YAML
that Statelock's schema accepts.

It is a local development tool. It runs from a clone of this repository, is not
part of the `statelock-ai` package or the proxy, and does not enforce anything.
You test a draft the way you run any policy: load it into Statelock and run
your agent (see [Test the draft](#test-the-draft)).

## Run it

Natively, from the repository root (Python 3.10+):

```bash
pip install -e ".[policy-studio]"
export STATELOCK_POLICY_STUDIO_API_KEY=your-provider-key   # default model: a key from https://aistudio.google.com/apikey
export STATELOCK_POLICY_STUDIO_MODEL=provider/model        # optional; default gemini/gemini-3.8-flash
export STATELOCK_POLICY_STUDIO_API_BASE=http://host:port   # optional; e.g. a local Ollama server
python tools/policy-studio/server.py
```

Open http://127.0.0.1:8765/.

With Docker, set the same variables in the shell, then:

```bash
docker compose -f compose.dev.yaml --profile policy-studio up --build policy-studio
```

The profile starts only Policy Studio, not the proxy. Set
`STATELOCK_POLICY_STUDIO_PORT` if port 8765 is taken. Recreate the container
after changing its settings.

The model is a [LiteLLM](https://docs.litellm.ai/) name; its prefix selects the
provider. Policy Studio passes `STATELOCK_POLICY_STUDIO_API_KEY` to LiteLLM
and ignores provider variables such as `GEMINI_API_KEY`, so change the model
and key together. `ollama/` and `ollama_chat/` models need no key; from Docker,
`STATELOCK_POLICY_STUDIO_API_BASE=http://host.docker.internal:11434` reaches
Ollama on the host. The server never reads `.env`.

At startup the terminal shows the model, API base, LiteLLM version, whether a
key is set and a short SHA-256 fingerprint of it (never the key). Startup does
not contact the model. A failed model request reports its HTTP status and known
provider error codes, never the provider's raw message.

Custom rules: the server loads `statelock.rules` entry points and
`STATELOCK_RULE_MODULES` at startup, as the proxy does, and tells the model
about them. A module that fails to load stops the server. The Docker service
loads the OJS demo's rule module (`examples/ojs/ojs_rules.py`), as the dev
proxy does.

## Record a task in Chrome

1. Do the task on a test site with test accounts, never with real credentials
   or personal data. Log in before recording if the login is not part of what
   the policy should check.
2. Open DevTools, then the Command Menu (`Ctrl`+`Shift`+`P`, or `Command`+`Shift`+`P`
   on macOS), type `Recorder` and open the **Recorder** panel. Create and start
   a recording.
3. Do the task once, successfully, then stop the recording.
4. Export it as **JSON** (not a Puppeteer script). Keep the file private: it
   contains everything you typed, including passwords.
5. Record each wrong run the policy must block the same way, as its own
   recording: the wrong button, another record's page, a wrong value typed.
   Chrome performs these actions for real, so use a test site you can reset.

## Draft a policy

1. **Load recording** and choose the JSON file of a correct run. The timeline
   shows the actions; **All events** also shows key and viewport events. Typed
   values are not shown.
2. **Add recording** for each wrong run. A recording is **must pass** (every
   action allowed) or **must be blocked**; for a run that must be blocked, say
   why, select the step where it must be stopped, and click **Must be blocked at
   this step**. At least one recording must pass.
3. Select a step, choose **Before action** or **After action**, and write what
   must be true. Add as many checks as you need, on any steps of any recording.
4. Discuss them in the chat. Every message first opens a preview of exactly
   what will be sent; nothing goes to the model until you click **Send to model**.
5. When the checks are clear, click **Draft YAML** and enter the `agent_id`
   your agent uses and, optionally, a page URL scope.
6. If the draft passes Statelock's schema check, **Download YAML**. Checks the
   model cannot express are marked as needing manual work, and the chat lists,
   for each run that must be blocked, the rule the model says stops it.

**Export draft session** saves the checks, the recordings' roles and the chat,
not the recordings. **Load draft session** then asks for each recording in turn.

## What is sent to the model

The raw recordings never leave the browser, and neither do their titles. A
request contains:

- each recording's number and role: must pass, or must be blocked, with your
  reason and its block step;
- each check: its text, recording number, step number, step type and phase
  (before or after);
- a summary of each annotated step and each block step, derived in the browser:
  - `label`: the step's label in the timeline, such as "Clicked Decline Submission";
  - `selectors`: the target's selectors as Chrome recorded them (`aria/`, `text/`,
    `xpath/`, `pierce/` or CSS; shadow-root parts joined by ` >>> `), at most 8,
    each at most 300 characters;
  - `page_url`: the page the step happened on, from the latest navigation
    before it (empty when unknown);
  - `navigates_to`: where the step led, if it navigated;
  - `frame`: whether the target is in an iframe;
  - `key`: for key steps, the key name;
- the agent ID and page scope, once you ask for a draft;
- the conversation so far;
- instructions on Statelock's YAML, with the rules this installation has
  (built-in and custom), generated from the code at startup.

URLs are http(s) only, without user info, query or fragment. Typed form values
are never sent: no field of a recorded step is sent other than those above, and
a selector, label or URL that contains a value typed in any of the recordings
(of 4 or more characters) is left out. Your own notes and messages can still contain sensitive data, and
a URL path can contain an ID. The preview shows the whole request, including
the summaries; review it before sending.

## Test the draft

The YAML is checked against Statelock's policy schema with the same agent ID,
so it loads, but Policy Studio does not run it, and the model's account of what
blocks each wrong run may be wrong. Test it as you would any policy: Statelock
loads policies only at startup, so start it with the draft, then run your agent.

1. Save the draft under a name ending in `.draft.yaml` (ignored by git).
2. Start Statelock with it, in place of the policy it replaces: as
   `STATELOCK_POLICY_FILE`, or in `STATELOCK_EXTRA_POLICY_FILES` instead of the
   agent's current file. Loading both would give the agent two policies.
   Include the rule modules the draft uses in `STATELOCK_RULE_MODULES`.
3. Run your agent on each correct case: every action must be allowed.
4. Run it on each case that must be blocked: the session must end at the step
   you marked, with the violation naming the rule you expected (the agent's
   error, Statelock's log, the violation record, or the Enclave dashboard).
5. Restart Statelock with the policy you had before.

[`examples/ojs/POLICY_STUDIO.md`](../../examples/ojs/POLICY_STUDIO.md) does this
for the OJS demo, with the commands.

## Limits

The page and the server enforce the same limits; the page says which one a
request exceeds.

| What | Limit |
| --- | --- |
| Recordings | 10 per draft; each file 5 MB, 1 to 2000 steps |
| Checks | 200 across all recordings, each up to 2000 characters; a block reason up to 2000 |
| Chat message | 4000 characters |
| Conversation | 16 exchanges; **New conversation** clears the chat and draft, keeping the checks |
| Agent ID / page scope | 200 / 2000 characters |
| Step summary | label 300 characters (shortened), 8 selectors of up to 300 characters, URLs up to 1000 characters (longer ones are left out) |
| Model reply | 64 KiB; 90 s timeout |

## Security model

- The server listens on 127.0.0.1 and accepts only `127.0.0.1` and
  `localhost` as Host, so other machines and DNS-rebinding pages cannot use it.
- The API accepts only same-origin requests carrying a random token that the
  server generates at startup and embeds in the page it serves. Reload the page
  after restarting the server.
- The page allows no inline or third-party scripts (Content-Security-Policy),
  and responses are not cached.
- Under Docker it listens on 0.0.0.0 inside a network of its own, published
  only on the host's loopback, so the demo agents cannot reach it or spend the
  model key.
- The key stays in the server's environment; the browser never sees it.

## Save and resume

**Export draft session** saves a JSON file with the checks, agent ID, page
scope and conversation, plus the recording's title and step types. It does not
contain the recording, its typed values, the step summaries (they are derived
again from the recording) or the YAML draft.

To resume, click **Load draft session** and then choose the original
recording, or load the recording first. The session must match the recording's
title and step types. Ask for a new draft after loading: a draft is never
restored without a fresh schema check.

## Development

```bash
python -m pytest -q tools/policy-studio/test/test_draft_with_model.py   # server; LiteLLM is stubbed
node --test tools/policy-studio/test/*.test.js                          # browser logic (core.js)
```

`server.py` serves the page and the API; `draft_with_model.py` holds the
request checks, the model instructions and the schema check; `core.js` holds
the browser-side logic (recording parsing, step summaries, request limits,
sessions) and `studio.js` the page wiring.
