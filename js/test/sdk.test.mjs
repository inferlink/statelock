// SDK behaviour that needs no browser: upload buffers, download waits, responses, guard lookups.
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { test } from "node:test";

import {
  StatelockDownloadError,
  StatelockPolicyViolationError,
  StatelockRequestError,
  StatelockResponse,
  guard,
  statelockFetch,
  uploadFiles,
  waitForDownload,
} from "../dist/index.js";
import { frameDownloads, settledGuids } from "../dist/cdp.js";
import { decodeViolation } from "../dist/violations.js";

/** A CDP session that records uploaded bytes and answers Statelock.downloads with `downloads`. */
function fakeCdp(downloads = []) {
  const chunks = [];
  return {
    chunks,
    async send(method, params) {
      if (method === "Statelock.uploadFileBegin") return { uploadId: "u" };
      if (method === "Statelock.uploadFileChunk") chunks.push(Buffer.from(params.data, "base64"));
      if (method === "Statelock.uploadFileEnd") return { path: "/p", name: "x", size: 0, sha256: "", mimeType: null };
      if (method === "Statelock.downloads") return { downloads: typeof downloads === "function" ? downloads() : downloads };
      return {};
    },
  };
}

test("upload buffers: bytes of any view, text as UTF-8", async () => {
  const cases = [
    [Buffer.from("ab"), "ab"],
    [new Uint16Array([0x6261]), "ab"], // byte for byte, not element by element
    [new DataView(new Uint8Array([0x61, 0x62, 0x63]).buffer, 1), "bc"], // a view's own bytes only
    [new Uint8Array([0x61, 0x62]).buffer, "ab"],
    ["é", "é"],
  ];
  for (const [buffer, expected] of cases) {
    const cdp = fakeCdp();
    await uploadFiles(cdp, { name: "x", buffer });
    assert.equal(Buffer.concat(cdp.chunks).toString("utf8"), expected, String(buffer));
  }
});

test("an upload buffer of another type, or none, is a TypeError", async () => {
  for (const buffer of [undefined, null, 7, { length: 2 }, ["a"]]) {
    const cdp = fakeCdp();
    await assert.rejects(uploadFiles(cdp, { name: "x", buffer }), TypeError);
    assert.equal(cdp.chunks.length, 0);
  }
});

test("a blocked download reports the rule Statelock gives, or null", async () => {
  for (const [entry, rule] of [[{ rule: "restrict_downloads" }, "restrict_downloads"], [{}, null]]) {
    const cdp = fakeCdp([{ guid: "g", state: "blocked", name: "tool.exe", reason: "not allowed", ...entry }]);
    await assert.rejects(waitForDownload(cdp, new Set(), 1000), (error) => {
      assert.ok(error instanceof StatelockPolicyViolationError);
      assert.equal(error.rule, rule);
      assert.equal(error.reason, "not allowed");
      return true;
    });
  }
});

test("waitForDownload: a timeout of 0 waits without a limit, as everywhere else", async () => {
  let polls = 0;
  const done = { guid: "g", state: "completed", name: "r.pdf", url: "u", size: 1, sha256: "s" };
  const cdp = fakeCdp(() => (++polls > 3 ? [done] : [])); // completes on the fourth poll (~300 ms)
  assert.equal((await waitForDownload(cdp, new Set(), 0)).guid, "g");
  await assert.rejects(waitForDownload(fakeCdp(), new Set(), 150), StatelockDownloadError);
});

test("StatelockResponse has Playwright's APIResponse methods", async () => {
  const response = new StatelockResponse({
    status: 201,
    statusText: "Created",
    url: "https://x/y",
    headers: [["Content-Type", "application/json"], ["Set-Cookie", "a=1"], ["Set-Cookie", "b=2"]],
    body: Buffer.from('{"a":1}').toString("base64"),
  });
  assert.equal(response.status(), 201);
  assert.equal(response.statusText(), "Created");
  assert.equal(response.url(), "https://x/y");
  assert.equal(response.ok(), true);
  assert.equal(response.headers()["content-type"], "application/json");
  assert.equal(response.headersArray().filter(({ name }) => name === "Set-Cookie").length, 2);
  assert.deepEqual(await response.json(), { a: 1 });
  assert.equal(await response.text(), '{"a":1}');
  assert.deepEqual(await response.body(), Buffer.from('{"a":1}'));
  assert.equal(new StatelockResponse({ status: 404 }).ok(), false);
});

test("statelockFetch: form, params, failOnStatusCode and errors", async () => {
  const sent = [];
  let answer = { status: 200 };
  const cdp = {
    async send(method, params) {
      sent.push(params);
      if (answer instanceof Error) throw answer;
      return answer;
    },
  };
  await statelockFetch(cdp, "https://x/y?a=1#f", { method: "post", form: { b: "1 2" }, params: { c: 3 } });
  assert.equal(sent[0].url, "https://x/y?a=1&c=3#f");
  assert.equal(sent[0].method, "POST");
  assert.equal(sent[0].headers["Content-Type"], "application/x-www-form-urlencoded");
  assert.equal(Buffer.from(sent[0].body, "base64").toString(), "b=1+2");
  await statelockFetch(cdp, "https://x/y", { params: "d=4", data: { e: 5 }, headers: { "content-type": "text/x" } });
  assert.equal(sent[1].url, "https://x/y?d=4");
  assert.deepEqual(sent[1].headers, { "content-type": "text/x" });
  answer = { status: 404, statusText: "Not Found", url: "https://x/y" };
  assert.equal((await statelockFetch(cdp, "https://x/y")).status(), 404);
  await assert.rejects(statelockFetch(cdp, "https://x/y", { failOnStatusCode: true }), StatelockRequestError);
  answer = new Error("Statelock declined the request: no request_access rule");
  await assert.rejects(statelockFetch(cdp, "https://x/y"), (error) => error instanceof StatelockRequestError);
  answer = new Error('STATELOCK_POLICY_VIOLATION {"rule":"secret_injection"}');
  await assert.rejects(statelockFetch(cdp, "https://x/y"), (error) => error === answer); // guard() converts it
});

test("frameDownloads: a page's completed downloads, by frame", () => {
  const entry = (guid, frameId, state = "completed") =>
    ({ guid, frameId, state, name: `${guid}.pdf`, url: "u", size: 1, sha256: "s" });
  const entries = [
    entry("seen", "F1"), entry("mine", "F1"), entry("child", "F2"), entry("other", "X"),
    entry("running", "F1", "in_progress"), entry("blocked", "F1", "blocked"), entry("unframed", null),
  ];
  const split = (unattributed) => frameDownloads(entries, new Set(["seen"]), new Set(["F1", "F2"]), unattributed);
  let { mine, others } = split(false);
  assert.deepEqual(mine.map((d) => d.guid), ["mine", "child"]);
  assert.deepEqual(mine[0], { guid: "mine", name: "mine.pdf", url: "u", size: 1, sha256: "s", frameId: "F1" });
  assert.deepEqual(others.map((d) => d.guid), ["other", "unframed"]);
  ({ mine, others } = split(true));
  assert.deepEqual(mine.map((d) => d.guid), ["mine", "child", "unframed"]);
  assert.deepEqual(others.map((d) => d.guid), ["other"]);
});

test("decodeViolation reads the full and the compact form", () => {
  assert.deepEqual(decodeViolation('x STATELOCK_POLICY_VIOLATION {"rule":"r","reason":"a {b}"} y'), { rule: "r", reason: "a {b}" });
  assert.deepEqual(decodeViolation('STATELOCK_POLICY_VIOLATION {"t":"pre","r":"x","s":"id","n":3}'), {
    violation_type: "pre_condition", rule: "x", session_id: "id", sequence: 3,
  });
  assert.equal(decodeViolation("nothing"), null);
});

/** A stand-in for GET /violations/<id>: answers with `answer` and counts requests. */
async function violationServer(answer) {
  const stub = { requests: 0, answer };
  const server = createServer((request, response) => {
    stub.requests++;
    const [status, body] = stub.answer;
    response.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(body));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  stub.url = `http://127.0.0.1:${server.address().port}`;
  stub.close = () => new Promise((resolve) => server.close(resolve));
  return stub;
}

const RECORD = { rule: "restrict_downloads", reason: "tool.exe: .exe is not allowed", session_id: "s-1", agent_id: "a" };

test("guard: a failed lookup keeps the known violation", async () => {
  const stub = await violationServer([500, { detail: "down" }]);
  try {
    const blocked = new StatelockPolicyViolationError({ rule: "restrict_downloads", reason: "tool.exe" });
    const options = { serverUrl: stub.url, sessionId: "s-1", apiKey: "k", watchIntervalMs: 0 };
    await assert.rejects(guard(() => Promise.reject(blocked), options), (error) => error === blocked);
    assert.equal(stub.requests, 1);
    stub.answer = [200, RECORD];
    await assert.rejects(guard(() => Promise.reject(blocked), options), (error) => {
      assert.equal(error.agentId, "a"); // completed with the record
      assert.equal(error.cause, blocked);
      return true;
    });
  } finally {
    await stub.close();
  }
});

test("guard: a violation the watcher found is not looked up again", async () => {
  const stub = await violationServer([200, RECORD]);
  try {
    const options = { serverUrl: stub.url, sessionId: "s-1", apiKey: "k", watchIntervalMs: 20 };
    const hang = () => new Promise(() => {}); // a framework call that never settles
    await assert.rejects(guard(hang, options), (error) => {
      assert.ok(error instanceof StatelockPolicyViolationError);
      assert.equal(error.rule, "restrict_downloads");
      return true;
    });
    assert.equal(stub.requests, 1);
    // An outer guard does not ask again for the inner guard's violation either.
    await assert.rejects(guard(() => guard(hang, options), { ...options, watchIntervalMs: 0 }), StatelockPolicyViolationError);
    assert.equal(stub.requests, 2);
  } finally {
    await stub.close();
  }
});

test("settledGuids: only finished downloads are old to a new listener", () => {
  const entries = ["completed", "blocked", "canceled", "in_progress", "checking"].map((state) => ({ guid: state, state }));
  assert.deepEqual([...settledGuids(entries)].sort(), ["blocked", "canceled", "completed"]);
});
