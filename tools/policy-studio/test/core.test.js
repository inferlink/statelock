// SPDX-License-Identifier: Apache-2.0
const assert = require("node:assert/strict");
const test = require("node:test");
const core = require("../core.js");

// The server's values (draft_with_model.LIMITS); test_draft_with_model.py checks the page serves them.
const limits = {max_steps: 2000, max_runs: 10, max_notes: 200, max_note_chars: 2000, max_label_chars: 300, max_selectors: 8,
  max_selector_chars: 300, max_url_chars: 1000, max_key_chars: 40, max_message_chars: 4000,
  max_reply_chars: 65536, max_turns: 16, max_agent_id_chars: 200, max_page_scope_chars: 2000,
  max_body_bytes: 13961136};

const sample = {
  title: "OJS Reject 102",
  steps: [
    {type: "setViewport", width: 800},
    {type: "navigate", url: "http://localhost/login"},
    {type: "change", selectors: [["aria/Password"]], value: "demo-secret"},
    {type: "click", selectors: [["aria/Decline Submission"]], assertedEvents: [{type: "navigation", url: "http://localhost/workflow/102"}]},
  ],
};
const reply = JSON.stringify({reply: "Which paper?", questions: ["One paper or all?"], mappings: [], yaml: null});
const turn = (user = "Review these checks.", draftRequested = false) => ({user, assistant: reply, draft_requested: draftRequested});
const notes = [{run: 0, stepIndex: 3, phase: "before", text: "Decision must permit decline."}];
const pass = (recording = sample) => ({recording, kind: "pass", description: "", blockStep: null});
const runs = [pass()];
const session = (overrides = {}) => JSON.stringify({...core.exportSession(runs, notes, "ojs_agent", "/journal/", [turn()]), ...overrides});
const request = (overrides = {}) => ({context: core.draftContext(runs, notes, "ojs_agent", "", limits), turns: [],
  message: "Review", draft_requested: false, ...overrides});

test("validates Chrome recording shape", () => {
  assert.deepEqual(core.parseRecording(JSON.stringify(sample), limits), sample);
  assert.throws(() => core.parseRecording("not json", limits), /valid JSON/);
  assert.throws(() => core.parseRecording('{"steps":[]}', limits), /1 to 2000/);
  assert.throws(() => core.parseRecording('{"title":42,"steps":[{"type":"click"}]}', limits), /title must be text/);
});

test("makes action labels without showing typed values", () => {
  assert.equal(core.stepLabel(sample.steps[2]), "Changed Password");
  assert.equal(core.stepLabel(sample.steps[3]), "Clicked Decline Submission");
  assert.equal(core.stepLabel({type: "click", frame: [0], selectors: [["html"]]}), "Focused embedded editor");
  assert.equal(core.destination(sample.steps[3]), "http://localhost/workflow/102");
  assert.deepEqual(core.visibleStepIndexes(sample, [], false), [1, 2, 3]);
  assert.deepEqual(core.visibleStepIndexes(sample, [{stepIndex: 0}], false), [0, 1, 2, 3]);
  assert.equal(core.preferredSelector, undefined);
});

test("session export omits recorded input values and can be reloaded", () => {
  const exported = session();
  assert.equal(exported.includes("demo-secret"), false);
  assert.deepEqual(core.parseSession(exported, [sample], limits),
    {runs: [{kind: "pass", description: "", blockStep: null}], notes, turns: [turn()], agentId: "ojs_agent", pageScope: "/journal/"});
  assert.throws(() => core.parseSession(session({notes: [{...notes[0], stepIndex: 99}]}), [sample], limits), /invalid check/);
  assert.throws(() => core.parseSession(session({notes: [{...notes[0], run: 1}]}), [sample], limits), /invalid check/);
  assert.throws(() => core.parseSession(session({notes: [null]}), [sample], limits), /invalid check/);
  assert.throws(() => core.parseSession(session({schema: "statelock-policy-notes/2"}), [sample], limits), /draft session file/);
  assert.throws(() => core.readSession("{"), /not valid JSON/);
});

test("a session matches its recordings, in order, by title and every step type", () => {
  const swapped = {...sample, steps: [sample.steps[0], sample.steps[1], sample.steps[3], sample.steps[2]]};
  assert.throws(() => core.parseSession(session(), [swapped], limits), /different recordings/);
  assert.throws(() => core.parseSession(session(), [{...sample, title: "Other"}], limits), /different recordings/);
  assert.throws(() => core.parseSession(session(), [sample, sample], limits), /different recordings/);
  assert.throws(() => core.parseSession(session({recordings: undefined}), [sample], limits), /invalid recordings/);
  assert.throws(() => core.parseSession(session({recordings: [{title: "x"}]}), [sample], limits), /invalid recordings/);
});

const blocked = {title: "OJS wrong paper", steps: [{type: "navigate", url: "http://localhost/workflow/101"},
  {type: "change", selectors: [["#email"]], value: "another email"},
  {type: "click", selectors: [["aria/Decline Submission"]]}]};
const block = {recording: blocked, kind: "block", description: "The page shows paper 101.", blockStep: 2};

test("recordings must pass or be blocked at a step, for a stated reason", () => {
  assert.equal(core.runsProblem([pass(), block], limits), "");
  assert.match(core.runsProblem([], limits), /1 to 10 recordings/);
  assert.match(core.runsProblem([block], limits), /at least one recording as must pass/);
  assert.match(core.runsProblem([pass(), {...block, description: " "}], limits), /Recording 2: say why/);
  assert.match(core.runsProblem([pass(), {...block, description: "x".repeat(2001)}], limits), /limited to 2000/);
  assert.match(core.runsProblem([pass(), {...block, blockStep: null}], limits), /Recording 2: choose the step/);
  assert.match(core.runsProblem(Array.from({length: 11}, () => pass()), limits), /1 to 10 recordings/);
  assert.equal(core.runLabel(block, 1), "2. Must block: OJS wrong paper");
  assert.deepEqual(core.visibleStepIndexes(sample, [], false, 0), [0, 1, 2, 3]);
});

test("a session restores each recording's role and the checks on every recording", () => {
  const both = [pass(), block];
  const checks = [...notes, {run: 1, stepIndex: 2, phase: "before", text: "The paper matches the URL."}];
  const text = JSON.stringify(core.exportSession(both, checks, "ojs_agent", "", []));
  assert.equal(text.includes("another email"), false);
  const restored = core.parseSession(text, [sample, blocked], limits);
  assert.deepEqual(restored.runs, [{kind: "pass", description: "", blockStep: null},
    {kind: "block", description: "The page shows paper 101.", blockStep: 2}]);
  assert.deepEqual(restored.notes, checks);
  const saved = JSON.parse(text);
  for (const role of [{kind: "maybe"}, {blockStep: 3}, {blockStep: -1}, {description: 4}]) {
    const bad = {...saved, recordings: [saved.recordings[0], {...saved.recordings[1], ...role}]};
    assert.throws(() => core.parseSession(JSON.stringify(bad), [sample, blocked], limits), /invalid recording role/,
      JSON.stringify(role));
  }
  const passWithStep = {...saved, recordings: [{...saved.recordings[0], blockStep: 1}, saved.recordings[1]]};
  assert.throws(() => core.parseSession(JSON.stringify(passWithStep), [sample, blocked], limits), /invalid recording role/);
});

test("model context gives each run's role and block step, never titles or typed values", () => {
  const context = core.draftContext([pass(), block], [{run: 1, stepIndex: 2, phase: "before", text: "Paper matches."}],
    "ojs_agent", "", limits);
  assert.deepEqual(context.runs, [
    {run: 1, kind: "pass", description: "", block_step: null},
    {run: 2, kind: "block", description: "The page shows paper 101.", block_step: {step: 3, step_type: "click",
      step_summary: {label: "Clicked Decline Submission", selectors: ["aria/Decline Submission"],
        page_url: "http://localhost/workflow/101", navigates_to: "", frame: false}}},
  ]);
  assert.equal(context.notes[0].run, 2);
  const wire = JSON.stringify(context);
  for (const hidden of ["OJS wrong paper", "OJS Reject 102", "another email", "demo-secret"]) {
    assert.equal(wire.includes(hidden), false, hidden);
  }
  // A value typed in one recording is hidden from every recording's summaries.
  const leaky = {title: "t", steps: [{type: "click", selectors: [["text/another email"], ["#ok"]]}]};
  const mixed = core.draftContext([pass(leaky), block], [{run: 0, stepIndex: 0, phase: "before", text: "x"}], "a", "", limits);
  assert.deepEqual(mixed.notes[0].step_summary.selectors, ["#ok"]);
});

test("a session is rejected whole when any saved turn is unusable", () => {
  const bad = [
    {...turn(), assistant: null},
    {...turn(), assistant: "not json"},
    {...turn(), assistant: "null"},
    {...turn(), assistant: JSON.stringify({reply: 3, questions: []})},
    {...turn(), assistant: JSON.stringify({reply: "ok", questions: "one"})},
    {...turn(), user: ""},
    {...turn(), user: "x".repeat(limits.max_message_chars + 1)},
    {user: "hi", assistant: reply},
    {...turn(), extra: true},
  ];
  for (const item of bad) {
    assert.throws(() => core.parseSession(session({studioChat: [item]}), [sample], limits), /invalid chat/, JSON.stringify(item));
  }
  // A draft request may carry no message of its own, as the server allows.
  assert.equal(core.isTurn(turn("", true), limits), true);
  assert.throws(() => core.parseSession(session({agentId: 7}), [sample], limits), /policy settings/);
  assert.throws(() => core.parseSession(session({agentId: "a".repeat(201)}), [sample], limits), /policy settings/);
});

test("a full conversation can be saved and reloaded but not continued", () => {
  const full = Array.from({length: limits.max_turns}, () => turn());
  assert.equal(core.parseSession(session({studioChat: full}), [sample], limits).turns.length, limits.max_turns);
  assert.throws(() => core.parseSession(session({studioChat: [...full, turn()]}), [sample], limits), /invalid chat/);
  assert.equal(core.conversationFull(full.slice(1), limits), false);
  assert.equal(core.conversationFull(full, limits), true);
  core.checkRequest(request({turns: full.slice(1)}), limits);
  assert.throws(() => core.checkRequest(request({turns: full}), limits), /reached 16 turns. Start a new conversation/);
});

test("requests are checked against the server's limits with accurate messages", () => {
  core.checkRequest(request({message: "x".repeat(limits.max_message_chars)}), limits);
  core.checkRequest(request({message: "", draft_requested: true}), limits);
  assert.throws(() => core.checkRequest(request({message: "x".repeat(limits.max_message_chars + 1)}), limits),
    /limited to 4000 characters/);
  // Code points, as Python counts them: 4000 emoji are 8000 UTF-16 units.
  core.checkRequest(request({message: "\u{1F600}".repeat(limits.max_message_chars)}), limits);
  assert.throws(() => core.checkRequest(request({message: " "}), limits), /Write a question/);
  const many = Array.from({length: limits.max_notes + 1}, () => notes[0]);
  assert.throws(() => core.checkRequest(request({context: core.draftContext(runs, many, "a", "", limits)}), limits), /1 to 200 checks/);
  const long = [{...notes[0], text: "x".repeat(limits.max_note_chars + 1)}];
  assert.throws(() => core.checkRequest(request({context: core.draftContext(runs, long, "a", "", limits)}), limits), /limited to 2000/);
  assert.throws(() => core.checkRequest(request(), {...limits, max_body_bytes: 10}), /too large/);
});

test("model context sends annotations with step summaries, never typed values", () => {
  const notes = [{run: 0, stepIndex: 2, phase: "after", text: "Login succeeded."}];
  const context = core.draftContext(runs, notes, "ojs_agent", "/journal/login", limits);
  assert.deepEqual(context, {agent_id: "ojs_agent", page_scope: "/journal/login",
    runs: [{run: 1, kind: "pass", description: "", block_step: null}],
    notes: [{run: 1, step: 3, step_type: "change", phase: "after", text: "Login succeeded.",
      step_summary: {label: "Changed Password", selectors: ["aria/Password"], page_url: "http://localhost/login",
        navigates_to: "", frame: false}}]});
  assert.equal(JSON.stringify(context).includes("demo-secret"), false);
});

const recorded = {
  title: "Summaries",
  steps: [
    {type: "navigate", url: "https://user:pw@shop.test/login?next=/a#top",
      assertedEvents: [{type: "navigation", url: "https://shop.test/login?next=/a"}]},
    {type: "change", value: "Hunter2-secret", text: "private-step-text", selectors: [["aria/Password"], ["#pw"]]},
    {type: "click", selectors: [["aria/Search[role=\"button\"]"], ["div#host", "button.go"], ["xpath///*[@id=\"go\"]"],
      ["pierce/#go"], ["text/Search"], ["text/Hunter2-secret"], ["#" + "x".repeat(301)], ["#a"], ["#a"], [""], [7]],
    assertedEvents: [{type: "navigation", url: "https://shop.test/results/hunter2-secret?q=1"}]},
    {type: "click", frame: [0], selectors: [["html"]]},
    {type: "keyDown", key: "Enter", selectors: [["#ignored"]]},
    {type: "click", selectors: Array.from({length: 12}, (_, i) => [`#s${i}`]),
      assertedEvents: [{type: "navigation", url: "https://shop.test/done/"}]},
    {type: "click", selectors: [["#after"]]},
    {type: "navigate", url: "file:///etc/passwd"},
    {type: "click", selectors: [["#local"]]},
  ],
};

test("step summaries carry clean URLs and recorded selectors, capped", () => {
  const summary = (index) => core.stepSummary(recorded, index, limits);
  assert.deepEqual(summary(0), {label: "Opened https://shop.test/login", selectors: [], page_url: "",
    navigates_to: "https://shop.test/login", frame: false});
  assert.deepEqual(summary(1), {label: "Changed Password", selectors: ["aria/Password", "#pw"],
    page_url: "https://shop.test/login", navigates_to: "", frame: false});
  // Kept as recorded, in order, deduplicated; too long, empty or non-text selectors are dropped, and so is
  // anything holding a typed value, including a URL path it appears in.
  assert.deepEqual(summary(2), {label: "Clicked Search", page_url: "https://shop.test/login", navigates_to: "", frame: false,
    selectors: ["aria/Search[role=\"button\"]", "div#host >>> button.go", "xpath///*[@id=\"go\"]", "pierce/#go",
      "text/Search", "#a"]});
  assert.deepEqual(summary(3), {label: "Focused embedded editor", selectors: ["html"], page_url: "", navigates_to: "",
    frame: true});
  assert.deepEqual(summary(4), {label: "Pressed Enter", selectors: ["#ignored"], page_url: "", navigates_to: "",
    frame: false, key: "Enter"});
  assert.equal(summary(5).selectors.length, limits.max_selectors);
  assert.equal(summary(5).navigates_to, "https://shop.test/done/");
  assert.equal(summary(6).page_url, "https://shop.test/done/");
  assert.equal(summary(8).page_url, "");
  assert.equal(core.stepSummary({steps: [{type: "keyUp", key: 5}]}, 0, limits).key, "");
  const long = core.stepSummary({steps: [{type: "navigate", url: "https://a.test/" + "p".repeat(2000)}]}, 0, limits);
  assert.equal([...long.label].length, limits.max_label_chars);
  assert.equal(long.navigates_to, "");
  const wire = JSON.stringify(core.draftContext([pass(recorded)], recorded.steps.map((_, stepIndex) => ({run: 0, stepIndex,
    phase: "before", text: "check"})), "a", "", limits));
  for (const hidden of ["Hunter2", "hunter2", "private-step-text", "pw@", "next=", "#top", "passwd"]) {
    assert.equal(wire.includes(hidden), false, hidden);
  }
});

test("URLs lose user info, query and fragment; other schemes are unknown", () => {
  assert.equal(core.cleanUrl("https://u:p@a.test:8443/x/y?z=1#f"), "https://a.test:8443/x/y");
  assert.equal(core.cleanUrl("http://a.test"), "http://a.test/");
  assert.equal(core.cleanUrl("https://a.test/caf\u00e9 x"), "https://a.test/caf%C3%A9%20x");
  for (const value of ["javascript:alert(1)", "data:text/html,x", "about:blank", "not a url", 5, undefined]) {
    assert.equal(core.cleanUrl(value), "", String(value));
  }
  assert.equal(core.cleanUrl("https://a.test/abc", 10), "");
});
