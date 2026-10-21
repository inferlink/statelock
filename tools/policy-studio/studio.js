// SPDX-License-Identifier: Apache-2.0
(function () {
  "use strict";

  const core = window.statelockStudioCore;
  const $ = (id) => document.getElementById(id);
  const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
  // Served by this server process: its limits, and the token its API requires.
  const limits = JSON.parse(meta("statelock-studio-limits"));
  const token = meta("statelock-studio-token");
  // runs: [{recording, kind: "pass"|"block", description, blockStep}]; current: the run shown.
  // Each note has run (its index in runs), stepIndex, phase and text.
  const state = {runs: [], current: 0, notes: [], selected: 0, showAll: false, nextId: 1,
    drafts: {}, turns: [], result: null, yaml: null, validationError: null,
    pending: null, pendingRequest: null, pendingSession: null, pendingRecordings: [],
    lastDraftRequested: false, dirty: false, busy: false, generation: 0, controller: null};
  $("note-text").maxLength = limits.max_note_chars;
  $("chat-message").maxLength = limits.max_message_chars;
  $("agent-id").maxLength = limits.max_agent_id_chars;
  $("page-scope").maxLength = limits.max_page_scope_chars;
  $("run-description").maxLength = limits.max_note_chars;
  const run = () => state.runs[state.current];
  const recording = () => run().recording;
  const runNotes = () => state.notes.filter((note) => note.run === state.current);
  const visible = () => core.visibleStepIndexes(recording(), runNotes(), state.showAll, run().blockStep);

  function text(tag, value, className) {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) element.className = className;
    return element;
  }
  function showError(message) { $("error").textContent = message; $("error").hidden = false; }
  function clearError() { $("error").textContent = ""; $("error").hidden = true; }
  function setBusy(busy) {
    state.busy = busy;
    $("review-request").disabled = busy; $("draft-yaml").disabled = busy;
  }
  // Abandons any model request in flight: its reply would describe a request that no longer applies.
  function stopRequest() {
    state.generation += 1;
    if (state.controller) state.controller.abort();
    state.controller = null;
    setBusy(false);
  }
  function invalidatePreview() {
    stopRequest();
    state.pending = null;
    state.pendingRequest = null;
    if ($("request-panel").open) $("request-panel").close();
    $("request-error").hidden = true;
    $("send-request").disabled = true;
  }
  function invalidateDraft() {
    state.result = null;
    state.yaml = null;
    state.validationError = null;
    state.lastDraftRequested = false;
    state.dirty = true;
    invalidatePreview();
    renderChat();
  }
  function noteChanged() { invalidateDraft(); renderTimeline(); }
  function draftDrafts() {
    const draft = $("note-text").value;
    if (draft.trim()) state.drafts[`${state.current}:${state.selected}`] = draft;
    else delete state.drafts[`${state.current}:${state.selected}`];
  }
  function addMeta(label, value) { if (value) $("step-meta").append(text("dt", label), text("dd", value)); }
  function selectStep(index) {
    if (index === state.selected) return;
    draftDrafts();
    state.selected = index;
    $("note-text").value = state.drafts[`${state.current}:${index}`] || "";
    renderTimeline();
    renderDetail();
  }
  function showRun(index) {
    draftDrafts();
    state.current = index;
    state.selected = visible()[0] ?? 0;
    $("note-text").value = state.drafts[`${state.current}:${state.selected}`] || "";
    renderRun(); renderTimeline(); renderDetail();
  }
  function renderRun() {
    const select = $("run-select");
    select.replaceChildren(...state.runs.map((item, index) => {
      const option = text("option", core.runLabel(item, index));
      option.value = String(index);
      return option;
    }));
    select.value = String(state.current);
    $("run-kind").value = run().kind;
    $("run-description").value = run().description;
    $("run-block").hidden = run().kind !== "block";
    $("block-step-label").textContent = run().blockStep === null ? "Not chosen: select the step, then mark it." :
      `Step ${run().blockStep + 1}: ${core.stepLabel(recording().steps[run().blockStep])}`;
    $("remove-recording").disabled = state.runs.length < 2;
    $("add-recording").disabled = state.runs.length >= limits.max_runs;
    $("recording-title").textContent = recording().title || "Untitled recording";
    $("step-count").textContent = `${recording().steps.length} recorded events`;
    $("sensitive-warning").hidden = !state.runs.some((item) =>
      item.recording.steps.some((step) => step.type === "change" && typeof step.value === "string"));
  }
  function runChanged() { invalidateDraft(); renderRun(); renderTimeline(); renderDetail(); }
  function renderTimeline() {
    const list = $("step-list");
    list.replaceChildren();
    for (const index of visible()) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "step-button";
      button.setAttribute("aria-current", index === state.selected ? "step" : "false");
      button.append(text("span", String(index + 1).padStart(2, "0"), "step-index"));
      const body = text("span", "", "step-body");
      body.append(text("span", core.stepLabel(recording().steps[index]), "step-label"));
      const count = runNotes().filter((note) => note.stepIndex === index).length;
      if (count) body.append(text("span", `${count} check${count === 1 ? "" : "s"}`, "step-checks"));
      if (index === run().blockStep) body.append(text("span", "Must be blocked here", "step-block"));
      button.append(body);
      button.addEventListener("click", () => selectStep(index));
      item.append(button);
      list.append(item);
    }
  }
  function renderNotes() {
    const list = $("notes-list");
    list.replaceChildren();
    const notes = runNotes().filter((note) => note.stepIndex === state.selected);
    $("check-count").textContent = `${notes.length} check${notes.length === 1 ? "" : "s"}`;
    if (!notes.length) list.append(text("p", "No checks attached to this step.", "no-checks"));
    for (const note of notes) {
      const card = text("div", "", "note");
      const row = text("div", "", "note-heading");
      row.append(text("span", note.phase === "before" ? "Before action" : "After action", "phase-label"));
      const remove = text("button", "Remove", "remove-button");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${note.phase} check`);
      remove.addEventListener("click", () => {
        state.notes = state.notes.filter((item) => item.id !== note.id);
        noteChanged(); renderNotes();
      });
      row.append(remove);
      const editor = document.createElement("textarea");
      editor.value = note.text;
      editor.rows = 2;
      editor.maxLength = limits.max_note_chars;
      editor.setAttribute("aria-label", `Edit ${note.phase} check`);
      editor.addEventListener("input", () => {
        note.text = editor.value;
        noteChanged();
        card.querySelector(".mapping-result")?.remove();
      });
      card.append(row, editor);
      const mapping = state.result?.mappings?.find((item) => item.note_index === state.notes.indexOf(note));
      if (mapping) {
        const label = {proposed: "Drafted", unresolved: "Needs clarification", not_enforceable: "Needs manual work"}[mapping.status];
        card.append(text("p", `${label}: ${mapping.explanation}`, "mapping-result"));
      }
      list.append(card);
    }
  }
  function renderDetail() {
    const step = recording().steps[state.selected];
    const indexes = visible();
    const position = indexes.indexOf(state.selected);
    $("previous-step").disabled = position <= 0;
    $("next-step").disabled = position >= indexes.length - 1;
    $("step-number").textContent = `Step ${state.selected + 1} of ${recording().steps.length}`;
    $("mark-block").hidden = run().kind !== "block";
    $("mark-block").textContent = run().blockStep === state.selected ? "Clear block step" : "Must be blocked at this step";
    $("step-title").textContent = core.stepLabel(step);
    $("step-meta").replaceChildren();
    addMeta("Event", step.type);
    addMeta("Page", step.type === "navigate" ? step.url : core.destination(step));
    if (Array.isArray(step.frame) && step.frame.length) addMeta("Frame", step.frame.join(" / "));
    const selectors = Array.isArray(step.selectors) ? step.selectors.flat().filter((value) => typeof value === "string") : [];
    $("selectors-panel").hidden = selectors.length === 0;
    $("selectors-panel").open = false;
    $("selector-list").replaceChildren(...selectors.map((selector) => text("li", selector)));
    renderNotes();
  }
  function renderChat() {
    const history = $("chat-history");
    history.replaceChildren();
    if (!state.turns.length) history.append(text("p", "Add checks to the timeline, then discuss them here.", "muted"));
    for (const turn of state.turns) {
      const asked = turn.draft_requested ? (turn.user ? `Draft request: ${turn.user}` : "Asked for a policy draft.") : turn.user;
      history.append(text("p", asked, "chat-user"));
      let answer;
      try { answer = JSON.parse(turn.assistant); } catch { continue; }
      const reply = text("div", "", "chat-assistant");
      reply.append(text("p", answer.reply));
      if (answer.questions?.length) {
        const questions = document.createElement("ul");
        for (const question of answer.questions) questions.append(text("li", question));
        reply.append(questions);
      }
      history.append(reply);
    }
    const status = $("draft-status");
    status.hidden = !state.result && !state.turns.length;
    if (!state.result && state.turns.length) {
      status.textContent = "Conversation saved. Review the current notes and ask for a fresh draft.";
    } else if (state.result) {
      const manualWork = [...state.result.mappings, ...state.result.blocks]
        .filter((mapping) => mapping.status === "not_enforceable").length;
      status.textContent = state.validationError ? "Draft failed schema validation. Ask the assistant to revise it." :
        state.yaml ? `Schema-valid draft. ${manualWork} check${manualWork === 1 ? " still needs" : "s still need"} manual work.` :
        state.result.questions.length ? `${state.result.questions.length} clarification question${state.result.questions.length === 1 ? "" : "s"} to answer.` :
        state.lastDraftRequested ? "No enforceable YAML was proposed. Review the annotations and chat." :
        "Ready to draft when the checks look right.";
    }
    const blocks = $("block-results");
    blocks.replaceChildren();
    const label = {proposed: "Blocked by the draft", unresolved: "Needs clarification", not_enforceable: "Not blocked: needs manual work"};
    for (const block of state.result?.blocks || []) {
      blocks.append(text("li", `Recording ${block.run}. ${label[block.status]}: ${block.explanation}`));
    }
    blocks.hidden = !blocks.children.length;
    $("yaml-panel").hidden = !state.yaml;
    $("yaml-preview").textContent = state.yaml || "";
    $("download-yaml").disabled = !state.yaml;
    $("new-conversation").disabled = !state.turns.length && !state.result;
    if (core.conversationFull(state.turns, limits)) {
      const full = `This conversation reached ${limits.max_turns} turns. Export it if needed, then start a new conversation.`;
      status.textContent = status.hidden ? full : `${status.textContent} ${full}`;
      status.hidden = false;
    }
  }
  function context() {
    if (!state.runs.length) throw new Error("Load a recording first.");
    const problem = core.runsProblem(state.runs, limits);
    if (problem) throw new Error(problem);
    if (!state.notes.length) throw new Error("Add at least one check to the timeline.");
    if (state.notes.some((note) => !note.text.trim())) throw new Error("Fill or remove empty checks.");
    if ($("note-text").value.trim() || Object.values(state.drafts).some((item) => item.trim())) {
      throw new Error("Add or clear unfinished check text first.");
    }
    return core.draftContext(state.runs, state.notes, $("agent-id").value, $("page-scope").value, limits);
  }
  // The server adds the drafting instruction; the message is only what the user typed.
  function payload(draftRequested) {
    const request = {context: context(), turns: state.turns, message: $("chat-message").value.trim(),
      draft_requested: draftRequested};
    core.checkRequest(request, limits);
    return request;
  }
  async function api(path, body, signal) {
    const response = await fetch(path, {method: "POST", signal, body: JSON.stringify(body),
      headers: {"Content-Type": "application/json", "X-Statelock-Studio-Token": token}});
    let value = null;
    try { value = await response.json(); } catch { /* a proxy or server error page: report the status */ }
    if (!response.ok) {
      throw new Error(typeof value?.detail === "string" ? value.detail :
        `Request failed (${response.status}${response.statusText ? ` ${response.statusText}` : ""}).`);
    }
    if (!value) throw new Error("The server sent an unreadable response.");
    return value;
  }
  function download(name, contents, mime) {
    const url = URL.createObjectURL(new Blob([contents], {type: mime}));
    const link = document.createElement("a");
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function readJsonFile(file) {
    if (!file || file.size > 5 * 1024 * 1024) throw new Error("Choose a JSON file smaller than 5 MB.");
    return file.text();
  }

  function clearPendingSession() {
    state.pendingSession = null;
    state.pendingRecordings = [];
    $("session-hint").hidden = true;
    $("empty-title").textContent = "Load a Chrome recording";
  }
  // Starts over with these recordings; the first must pass, the others default to must block.
  function startRuns(recordings) {
    state.runs = recordings.map((recording, index) =>
      ({recording, kind: index === 0 ? "pass" : "block", description: "", blockStep: null}));
    state.current = 0; state.notes = []; state.turns = []; state.result = null; state.yaml = null; state.validationError = null;
    state.lastDraftRequested = false; $("draft-settings").hidden = true;
    state.drafts = {}; state.dirty = false; state.nextId = 1; state.showAll = false;
    state.selected = visible()[0] ?? 0;
    $("note-text").value = ""; $("chat-message").value = "Review these checks and tell me what needs clarification.";
    $("agent-id").value = ""; $("page-scope").value = "";
    document.querySelector('input[name="step-view"][value="actions"]').checked = true;
    $("empty").hidden = true; $("workspace").hidden = false;
    $("export-session").disabled = false;
    invalidatePreview(); renderRun(); renderTimeline(); renderDetail(); renderChat();
  }
  // Applies a session that core.parseSession has already validated against state.runs' recordings.
  function restoreSession(session) {
    session.runs.forEach((saved, index) => Object.assign(state.runs[index], saved));
    state.notes = session.notes.map((note) => ({...note, id: state.nextId++}));
    state.turns = session.turns;
    state.result = null; state.yaml = null; state.validationError = null; state.lastDraftRequested = false;
    state.drafts = {}; state.dirty = false; $("draft-settings").hidden = true;
    $("agent-id").value = session.agentId;
    $("page-scope").value = session.pageScope;
    $("note-text").value = "";
    invalidatePreview(); showRun(0); renderChat();
  }
  function sessionHint() {
    const expected = core.sessionRecordings(core.readSession(state.pendingSession), limits);
    const next = expected[state.pendingRecordings.length];
    $("empty-title").textContent = `Choose recording ${state.pendingRecordings.length + 1} of ${expected.length}`;
    $("session-hint").textContent = `This draft session needs the Chrome recording "${next.title || "Untitled recording"}" next.`;
    $("session-hint").hidden = false;
    $("choose-recording").focus();
  }

  $("load-recording").addEventListener("click", () => $("recording-file").click());
  $("choose-recording").addEventListener("click", () => $("recording-file").click());
  $("add-recording").addEventListener("click", () => $("add-recording-file").click());
  $("load-session").addEventListener("click", () => $("session-file").click());
  $("recording-file").addEventListener("change", async (event) => {
    try {
      clearError();
      if (!state.pendingSession && state.dirty && !window.confirm("Replace these recordings and discard unexported work?")) return;
      const loaded = core.parseRecording(await readJsonFile(event.target.files[0]), limits);
      if (!state.pendingSession) return startRuns([loaded]);
      const saved = core.readSession(state.pendingSession);
      const expected = core.sessionRecordings(saved, limits);
      if (!core.matchesRecording(expected[state.pendingRecordings.length], loaded)) {
        throw new Error(`This is not the recording the draft session needs next ("${expected[state.pendingRecordings.length].title || "Untitled recording"}").`);
      }
      state.pendingRecordings.push(loaded);
      if (state.pendingRecordings.length < expected.length) return sessionHint();
      const recordings = state.pendingRecordings;
      let session;
      try { session = core.parseSession(state.pendingSession, recordings, limits); }
      catch (error) { clearPendingSession(); throw error; }
      clearPendingSession();
      startRuns(recordings);
      restoreSession(session);
    } catch (error) { showError(error.message || "Could not load recording."); }
    finally { event.target.value = ""; }
  });
  $("add-recording-file").addEventListener("change", async (event) => {
    try {
      clearError();
      if (state.runs.length >= limits.max_runs) throw new Error(`A draft holds at most ${limits.max_runs} recordings.`);
      const loaded = core.parseRecording(await readJsonFile(event.target.files[0]), limits);
      state.runs.push({recording: loaded, kind: "block", description: "", blockStep: null});
      invalidateDraft(); showRun(state.runs.length - 1);
    } catch (error) { showError(error.message || "Could not load recording."); }
    finally { event.target.value = ""; }
  });
  $("session-file").addEventListener("change", async (event) => {
    try {
      clearError();
      if (state.dirty && !window.confirm("Replace unexported work?")) return;
      const contents = await readJsonFile(event.target.files[0]);
      const saved = core.readSession(contents);
      const expected = core.sessionRecordings(saved, limits);
      // The loaded recordings are the session's: restore at once. Otherwise ask for each in turn.
      if (state.runs.length === expected.length &&
          expected.every((item, index) => core.matchesRecording(item, state.runs[index].recording))) {
        restoreSession(core.parseSession(contents, state.runs.map((item) => item.recording), limits));
        return;
      }
      state.pendingSession = contents; state.pendingRecordings = [];
      state.runs = []; state.notes = []; state.turns = []; state.dirty = false;
      invalidatePreview();
      $("workspace").hidden = true; $("empty").hidden = false; $("export-session").disabled = true;
      sessionHint();
    } catch (error) { showError(error.message || "Could not load draft session."); }
    finally { event.target.value = ""; }
  });
  $("run-select").addEventListener("change", () => showRun(Number($("run-select").value)));
  $("run-kind").addEventListener("change", () => {
    run().kind = $("run-kind").value;
    if (run().kind === "pass") run().blockStep = null;
    runChanged();
  });
  $("run-description").addEventListener("input", () => {
    run().description = $("run-description").value;
    invalidateDraft(); renderRun();
  });
  $("mark-block").addEventListener("click", () => {
    run().blockStep = run().blockStep === state.selected ? null : state.selected;
    runChanged();
  });
  $("remove-recording").addEventListener("click", () => {
    if (state.runs.length < 2) return;
    const count = runNotes().length;
    if (!window.confirm(`Remove this recording${count ? ` and its ${count} check${count === 1 ? "" : "s"}` : ""}?`)) return;
    const removed = state.current;
    state.runs.splice(removed, 1);
    state.notes = state.notes.filter((note) => note.run !== removed)
      .map((note) => (note.run > removed ? {...note, run: note.run - 1} : note));
    state.drafts = {};
    state.current = 0;
    invalidateDraft(); showRun(0);
  });
  document.querySelectorAll('input[name="step-view"]').forEach((radio) => radio.addEventListener("change", () => {
    state.showAll = radio.value === "all";
    const indexes = visible();
    if (!indexes.includes(state.selected)) selectStep(indexes[0]);
    renderTimeline(); renderDetail();
  }));
  function moveStep(direction) {
    const indexes = visible();
    const next = indexes[indexes.indexOf(state.selected) + direction];
    if (next !== undefined) selectStep(next);
  }
  $("previous-step").addEventListener("click", () => moveStep(-1));
  $("next-step").addEventListener("click", () => moveStep(1));
  $("note-form").addEventListener("submit", (event) => {
    event.preventDefault(); clearError();
    const value = $("note-text").value.trim();
    if (!value) return showError("Write a check before adding it.");
    if (state.notes.length >= limits.max_notes) return showError(`A draft holds at most ${limits.max_notes} checks.`);
    state.notes.push({id: state.nextId++, run: state.current, stepIndex: state.selected,
      phase: document.querySelector('input[name="phase"]:checked').value, text: value});
    $("note-text").value = ""; delete state.drafts[`${state.current}:${state.selected}`];
    noteChanged(); renderNotes();
  });
  $("note-text").addEventListener("input", () => { state.dirty = true; invalidatePreview(); });
  for (const id of ["agent-id", "page-scope", "chat-message"]) {
    $(id).addEventListener("input", () => {
      if (id !== "chat-message" && (state.turns.length || state.result)) { invalidateDraft(); renderNotes(); }
      else { state.dirty = true; invalidatePreview(); }
    });
  }
  async function preview(draftRequested) {
    try {
      clearError(); const request = payload(draftRequested);
      $("review-request").disabled = true; $("draft-yaml").disabled = true;
      const result = await api("/api/preview", request);
      if (JSON.stringify(request) !== JSON.stringify(payload(draftRequested))) {
        throw new Error("The request changed while preparing the preview. Review it again.");
      }
      state.pending = JSON.stringify(request);
      state.pendingRequest = request;
      $("request-model").textContent = `Configured model: ${result.model}`;
      $("request-data").textContent = JSON.stringify(request, null, 2);
      $("request-preview").textContent = JSON.stringify(result, null, 2);
      $("request-error").hidden = true;
      $("send-request").disabled = false;
      $("request-panel").showModal();
    } catch (error) { showError(error.message || "Could not preview request."); invalidatePreview(); }
    finally { setBusy(false); }
  }
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state.busy) preview(false);
  });
  $("draft-yaml").addEventListener("click", () => {
    invalidatePreview();
    $("draft-settings").hidden = false;
    if (!$("agent-id").value.trim()) {
      showError("Enter the Agent ID used by your existing agent, then click Draft YAML again.");
      $("agent-id").focus();
      return;
    }
    if (!state.busy) preview(true);
  });
  $("new-conversation").addEventListener("click", () => {
    if (!window.confirm("Start a new conversation? The chat and the current draft are cleared; your checks stay.")) return;
    clearError();
    state.turns = [];
    invalidateDraft(); renderNotes();
  });
  $("cancel-request").addEventListener("click", invalidatePreview);
  $("request-panel").addEventListener("cancel", (event) => {
    event.preventDefault();
    invalidatePreview();
  });
  $("send-request").addEventListener("click", async () => {
    if (state.busy) return;
    const generation = state.generation;
    const controller = new AbortController();
    try {
      clearError();
      const request = state.pendingRequest;
      if (!request || state.pending !== JSON.stringify(payload(request.draft_requested))) {
        throw new Error("The request changed. Review it again before sending.");
      }
      state.controller = controller;
      setBusy(true); $("send-request").disabled = true;
      const response = await api("/api/draft", request, controller.signal);
      // Cancelled, or the notes changed, while the model was answering.
      if (generation !== state.generation) return;
      state.turns.push({user: request.message, assistant: response.assistant, draft_requested: request.draft_requested});
      state.result = response.result; state.yaml = response.result.yaml; state.validationError = response.validation_error;
      state.lastDraftRequested = request.draft_requested;
      state.dirty = true;
      $("chat-message").value = "";
      invalidatePreview(); renderChat(); renderNotes();
      if (response.validation_error) showError(`Generated YAML failed schema validation: ${response.validation_error}. Ask the assistant to revise it.`);
    } catch (error) {
      if (generation !== state.generation) return;
      const message = error.message || "Could not draft policy.";
      showError(message);
      $("request-error").textContent = message;
      $("request-error").hidden = false;
    }
    finally {
      if (generation === state.generation) {
        state.controller = null;
        setBusy(false);
        if (state.pendingRequest) $("send-request").disabled = false;
      }
    }
  });
  $("export-session").addEventListener("click", () => {
    try {
      clearError();
      if ($("note-text").value.trim() || Object.values(state.drafts).some((item) => item.trim())) throw new Error("Add or clear unfinished check text first.");
      if (state.notes.some((note) => !note.text.trim())) throw new Error("Fill or remove empty checks.");
      const saved = core.exportSession(state.runs, state.notes, $("agent-id").value, $("page-scope").value, state.turns);
      download("statelock-policy-draft-session.json", JSON.stringify(saved, null, 2) + "\n", "application/json");
      state.dirty = false;
    } catch (error) { showError(error.message); }
  });
  $("download-yaml").addEventListener("click", () => {
    if (state.yaml) download("statelock-policy-draft.yaml", state.yaml.trimEnd() + "\n", "text/yaml");
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) { event.preventDefault(); event.returnValue = ""; }
  });
})();
