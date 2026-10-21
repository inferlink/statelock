// SPDX-License-Identifier: Apache-2.0
(function (root) {
  "use strict";

  // Limits come from the server (the page's statelock-studio-limits meta tag), so both sides agree.
  const SESSION_SCHEMA = "statelock-policy-notes/3";
  const RUN_KINDS = ["pass", "block"];
  const HIDDEN_TYPES = new Set(["setViewport", "keyDown", "keyUp"]);
  const KEY_TYPES = new Set(["keyDown", "keyUp"]);
  // Shorter typed values are too common to look for: they would hide most selectors and URLs.
  const MIN_TYPED_CHARS = 4;

  // Python's len() counts code points; String.length counts UTF-16 units.
  function chars(value) { return [...value].length; }
  function isText(value, limit) { return typeof value === "string" && value.trim() !== "" && chars(value) <= limit; }

  function parseRecording(text, limits) {
    let recording;
    try {
      recording = JSON.parse(text);
    } catch {
      throw new Error("This is not valid JSON.");
    }
    if (!recording || typeof recording !== "object" || !Array.isArray(recording.steps)) {
      throw new Error("Expected a Chrome Recorder JSON file with a steps array.");
    }
    if (recording.title !== undefined && typeof recording.title !== "string") {
      throw new Error("The recording title must be text.");
    }
    if (recording.steps.length === 0 || recording.steps.length > limits.max_steps) {
      throw new Error(`The recording must contain 1 to ${limits.max_steps} steps.`);
    }
    if (recording.steps.some((step) => !step || typeof step !== "object" || typeof step.type !== "string")) {
      throw new Error("Every recorded step needs a type.");
    }
    return recording;
  }

  function preferredSelector(step) {
    if (!Array.isArray(step.selectors)) return "";
    const selectors = step.selectors.flat().filter((value) => typeof value === "string");
    return selectors.find((value) => value.startsWith("aria/")) ||
      selectors.find((value) => value.startsWith("text/")) || selectors[0] || "";
  }

  function selectorLabel(selector) {
    if (selector.startsWith("aria/")) return selector.slice(5).replace(/\[role="[^"]+"\]$/, "");
    if (selector.startsWith("text/")) return selector.slice(5);
    if (selector === "body") return "editor body";
    return selector;
  }

  // An http(s) URL without user info, query or fragment, or "" (unknown); the server accepts only this form.
  function cleanUrl(value, maxChars = Infinity) {
    if (typeof value !== "string") return "";
    let url;
    try { url = new URL(value); } catch { return ""; }
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    url.username = ""; url.password = ""; url.search = ""; url.hash = "";
    const href = url.href;
    return /^https?:\/\/[!-~]+$/.test(href) && !/[?#]/.test(href) && href.length <= maxChars ? href : "";
  }

  // Each selector as Chrome recorded it; the parts of one selector path (shadow roots) joined by " >>> ".
  function recordedSelectors(step) {
    if (!Array.isArray(step.selectors)) return [];
    return step.selectors.flatMap((selector) => {
      const parts = Array.isArray(selector) ? selector : [selector];
      return parts.length && parts.every((part) => typeof part === "string" && part.trim()) ? [parts.join(" >>> ")] : [];
    });
  }

  // Values typed into form fields in any recording, plain and URL-encoded, lower case: nothing
  // sent may contain one.
  function typedValues(recordings) {
    return recordings.flatMap((recording) => recording.steps).flatMap((step) => {
      if (step.type !== "change" || typeof step.value !== "string") return [];
      const value = step.value.trim();
      return chars(value) >= MIN_TYPED_CHARS ? [value.toLowerCase(), encodeURIComponent(value).toLowerCase()] : [];
    });
  }

  function destination(step) {
    if (!Array.isArray(step.assertedEvents)) return "";
    const navigation = step.assertedEvents.find((event) => event && event.type === "navigation");
    return navigation && typeof navigation.url === "string" ? navigation.url : "";
  }

  function stepLabel(step) {
    const target = selectorLabel(preferredSelector(step));
    switch (step.type) {
      case "navigate": return `Opened ${cleanUrl(step.url) || "page"}`;
      case "click":
        if (Array.isArray(step.frame) && step.frame.length && target === "html") return "Focused embedded editor";
        return target ? `Clicked ${target}` : "Clicked an element";
      case "change": return target ? `Changed ${target}` : "Changed a field";
      case "keyDown": return `Pressed ${step.key || "key"}`;
      case "keyUp": return `Released ${step.key || "key"}`;
      case "setViewport": return "Set browser viewport";
      case "scroll": return "Scrolled page";
      default: return step.type;
    }
  }

  // notes: this recording's notes; blockStep: its block step, or null.
  function visibleStepIndexes(recording, notes, showAll, blockStep = null) {
    return recording.steps.flatMap((step, index) => {
      const marked = index === blockStep || notes.some((note) => note.stepIndex === index);
      return showAll || marked || !HIDDEN_TYPES.has(step.type) ? [index] : [];
    });
  }

  function runLabel(run, index) {
    const title = run.recording.title || "Untitled recording";
    return `${index + 1}. ${run.kind === "pass" ? "Must pass" : "Must block"}: ${title}`;
  }

  // What still keeps the recordings from being drafted, or "".
  function runsProblem(runs, limits) {
    if (!runs.length || runs.length > limits.max_runs) return `Load 1 to ${limits.max_runs} recordings.`;
    if (!runs.some((run) => run.kind === "pass")) return "Mark at least one recording as must pass.";
    for (const [index, run] of runs.entries()) {
      if (run.kind !== "block") continue;
      if (!run.description.trim()) return `Recording ${index + 1}: say why it must be blocked.`;
      if (chars(run.description) > limits.max_note_chars) {
        return `Recording ${index + 1}: the reason is limited to ${limits.max_note_chars} characters.`;
      }
      if (run.blockStep === null) return `Recording ${index + 1}: choose the step where it must be blocked.`;
    }
    return "";
  }

  function stepTypes(recording) { return recording.steps.map((step) => step.type); }

  function readSession(text) {
    let saved;
    try {
      saved = JSON.parse(text);
    } catch {
      throw new Error("This draft session is not valid JSON.");
    }
    if (!saved || typeof saved !== "object" || saved.schema !== SESSION_SCHEMA) {
      throw new Error("Expected a Statelock draft session file.");
    }
    return saved;
  }

  function checkMessage(message, draftRequested, limits) {
    if (typeof draftRequested !== "boolean") return "Invalid draft request.";
    if (typeof message !== "string" || chars(message) > limits.max_message_chars) {
      return `Messages are limited to ${limits.max_message_chars} characters.`;
    }
    if (!draftRequested && !message.trim()) return "Write a question or ask for a policy draft.";
    return "";
  }

  // Mirrors check_turns in draft_with_model.py, and guarantees what renderChat reads.
  function isTurn(turn, limits) {
    if (!turn || typeof turn !== "object" || Array.isArray(turn) ||
        Object.keys(turn).sort().join() !== "assistant,draft_requested,user" ||
        checkMessage(turn.user, turn.draft_requested, limits) ||
        !isText(turn.assistant, limits.max_reply_chars)) return false;
    let reply;
    try { reply = JSON.parse(turn.assistant); } catch { return false; }
    return !!reply && typeof reply === "object" && !Array.isArray(reply) && typeof reply.reply === "string" &&
      Array.isArray(reply.questions) && reply.questions.every((question) => typeof question === "string");
  }

  // The recordings a session needs, in order: [{title, stepTypes}].
  function sessionRecordings(saved, limits) {
    if (!Array.isArray(saved.recordings) || !saved.recordings.length || saved.recordings.length > limits.max_runs ||
        !saved.recordings.every((item) => item && typeof item === "object" && typeof item.title === "string" &&
          Array.isArray(item.stepTypes) && item.stepTypes.every((type) => typeof type === "string"))) {
      throw new Error("The draft session lists invalid recordings.");
    }
    return saved.recordings;
  }

  function matchesRecording(expected, recording) {
    const types = stepTypes(recording);
    return expected.title === (recording.title || "") && expected.stepTypes.length === types.length &&
      expected.stepTypes.every((type, index) => type === types[index]);
  }

  // Validates the whole file against its recordings, in order, before the caller changes any state.
  // Returns {runs: [{kind, description, blockStep}], notes, agentId, pageScope, turns}.
  function parseSession(text, recordings, limits) {
    const saved = readSession(text);
    const expected = sessionRecordings(saved, limits);
    if (expected.length !== recordings.length || !expected.every((item, index) => matchesRecording(item, recordings[index]))) {
      throw new Error("This draft session was exported for different recordings.");
    }
    const runs = expected.map((item, index) => {
      const steps = recordings[index].steps.length;
      if (!RUN_KINDS.includes(item.kind) || typeof item.description !== "string" ||
          chars(item.description) > limits.max_note_chars ||
          !(item.blockStep === null || (Number.isInteger(item.blockStep) && item.blockStep >= 0 && item.blockStep < steps)) ||
          (item.kind === "pass" && item.blockStep !== null)) {
        throw new Error("The draft session contains an invalid recording role.");
      }
      return {kind: item.kind, description: item.description, blockStep: item.blockStep};
    });
    if (!Array.isArray(saved.notes) || saved.notes.length > limits.max_notes) {
      throw new Error(`A draft session holds at most ${limits.max_notes} checks.`);
    }
    const notes = saved.notes.map((note) => {
      if (!note || typeof note !== "object" ||
          !Number.isInteger(note.run) || note.run < 0 || note.run >= recordings.length ||
          !Number.isInteger(note.stepIndex) || note.stepIndex < 0 ||
          note.stepIndex >= recordings[note.run].steps.length ||
          !["before", "after"].includes(note.phase) ||
          !isText(note.text, limits.max_note_chars)) {
        throw new Error("The draft session contains an invalid check.");
      }
      return {run: note.run, stepIndex: note.stepIndex, phase: note.phase, text: note.text.trim()};
    });
    if (!Array.isArray(saved.studioChat) || saved.studioChat.length > limits.max_turns ||
        !saved.studioChat.every((turn) => isTurn(turn, limits))) {
      throw new Error("The draft session contains an invalid chat.");
    }
    if (typeof saved.agentId !== "string" || chars(saved.agentId) > limits.max_agent_id_chars ||
        typeof saved.pageScope !== "string" || chars(saved.pageScope) > limits.max_page_scope_chars) {
      throw new Error("The draft session contains invalid policy settings.");
    }
    return {runs, notes, agentId: saved.agentId, pageScope: saved.pageScope,
      turns: saved.studioChat.map(({user, assistant, draft_requested}) => ({user, assistant, draft_requested}))};
  }

  // The recordings themselves are not saved: they may hold typed values.
  function exportSession(runs, notes, agentId, pageScope, turns) {
    return {
      schema: SESSION_SCHEMA,
      recordings: runs.map((run) => ({title: run.recording.title || "", stepTypes: stepTypes(run.recording),
        kind: run.kind, description: run.description.trim(), blockStep: run.blockStep})),
      notes: notes.map(({run, stepIndex, phase, text}) => ({run, stepIndex, phase, text: text.trim()})),
      agentId: agentId.trim(),
      pageScope: pageScope.trim(),
      studioChat: turns.map(({user, assistant, draft_requested}) => ({user, assistant, draft_requested})),
    };
  }

  // The server accepts MAX_TURNS - 1 earlier turns, so the reply is turn MAX_TURNS at most.
  function conversationFull(turns, limits) { return turns.length >= limits.max_turns; }

  // The same limits the server applies, with messages that say which one was hit.
  function checkRequest(request, limits) {
    if (conversationFull(request.turns, limits)) {
      throw new Error(`This conversation reached ${limits.max_turns} turns. Start a new conversation to continue.`);
    }
    const {runs, notes, agent_id: agentId, page_scope: pageScope} = request.context;
    if (!runs.length || runs.length > limits.max_runs) throw new Error(`Load 1 to ${limits.max_runs} recordings.`);
    if (!notes.length || notes.length > limits.max_notes) {
      throw new Error(`Add 1 to ${limits.max_notes} checks before drafting.`);
    }
    if (notes.some((note) => chars(note.text) > limits.max_note_chars)) {
      throw new Error(`Checks are limited to ${limits.max_note_chars} characters.`);
    }
    if (chars(agentId) > limits.max_agent_id_chars) throw new Error("Agent ID is too long.");
    if (chars(pageScope) > limits.max_page_scope_chars) throw new Error("Page scope is too long.");
    const problem = checkMessage(request.message, request.draft_requested, limits);
    if (problem) throw new Error(problem);
    if (new TextEncoder().encode(JSON.stringify(request)).length > limits.max_body_bytes) {
      throw new Error("This request is too large to send.");
    }
  }

  // The page a step happened on: the latest navigate step or navigation before it ("" when unknown).
  function pageBefore(recording, index) {
    let page = "";
    for (const step of recording.steps.slice(0, index)) {
      if (step.type === "navigate") page = cleanUrl(step.url);
      if (destination(step)) page = cleanUrl(destination(step));
    }
    return page;
  }

  // What the model learns about an annotated step. Only these derived fields: never a typed
  // value, a step's text, or any other recorded field.
  function stepSummary(recording, index, limits, typed = typedValues([recording])) {
    const step = recording.steps[index];
    const safe = (value) => !typed.some((secret) => value.toLowerCase().includes(secret));
    const url = (value) => {
      const clean = cleanUrl(value, limits.max_url_chars);
      return clean && safe(clean) ? clean : "";
    };
    let label = stepLabel(step);
    if (!safe(label)) label = step.type;
    if (chars(label) > limits.max_label_chars) label = [...label].slice(0, limits.max_label_chars - 1).join("") + "\u2026";
    const summary = {
      label,
      selectors: [...new Set(recordedSelectors(step))]
        .filter((selector) => chars(selector) <= limits.max_selector_chars && safe(selector))
        .slice(0, limits.max_selectors),
      page_url: url(pageBefore(recording, index)),
      navigates_to: url(destination(step) || (step.type === "navigate" ? step.url : "")),
      frame: Array.isArray(step.frame) && step.frame.length > 0,
    };
    if (KEY_TYPES.has(step.type)) {
      summary.key = typeof step.key === "string" && chars(step.key) <= limits.max_key_chars ? step.key : "";
    }
    return summary;
  }

  // runs: [{recording, kind, description, blockStep}]; notes: [{run, stepIndex, phase, text}].
  // Recording titles are not sent: the developer names each run's purpose in its description.
  function draftContext(runs, notes, agentId, pageScope, limits) {
    const typed = typedValues(runs.map((run) => run.recording));
    const stepOf = (recording, index) => ({step: index + 1, step_type: recording.steps[index].type,
      step_summary: stepSummary(recording, index, limits, typed)});
    return {
      runs: runs.map((run, index) => ({run: index + 1, kind: run.kind, description: run.description.trim(),
        block_step: run.kind === "block" && run.blockStep !== null ? stepOf(run.recording, run.blockStep) : null})),
      notes: notes.map((note) => {
        const {step, step_type, step_summary} = stepOf(runs[note.run].recording, note.stepIndex);
        return {run: note.run + 1, step, step_type, phase: note.phase, text: note.text.trim(), step_summary};
      }),
      agent_id: agentId.trim(),
      page_scope: pageScope.trim(),
    };
  }

  const api = {parseRecording, cleanUrl, destination, stepLabel, stepSummary, visibleStepIndexes, runLabel, runsProblem,
    readSession, sessionRecordings, matchesRecording, parseSession, exportSession, isTurn, conversationFull, checkRequest, draftContext};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.statelockStudioCore = api;
})(typeof window !== "undefined" ? window : globalThis);
