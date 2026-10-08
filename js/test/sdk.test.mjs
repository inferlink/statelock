// SDK behaviour that needs no browser: upload buffers, download waits, responses, guard lookups.
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { test } from "node:test";

import {
  StatelockDownloadError,
  StatelockPolicyViolationError,
  StatelockResponse,
  guard,
  uploadFiles,
  waitForDownload,
} from "../dist/index.js";
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
