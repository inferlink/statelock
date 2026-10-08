// The SDK against a running test proxy: session URLs, guards, violation lookups,
// credential injection, saved sessions and governed HTTP requests.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { chromium } from "playwright-core";

import {
  StatelockClientError,
  StatelockPolicyViolationError,
  StatelockResponse,
  createSessionUrl,
  deleteSavedSession,
  guard,
  listSavedSessions,
  secret,
  statelockFetch,
} from "../dist/index.js";
import { key, openSession, startServer } from "./helpers.mjs";

let server;
before(async () => { server = await startServer(); });
after(async () => { await server.stop(); });

test("a session URL opens a governed session, once", async () => {
  const { session, page, close } = await openSession(server);
  try {
    assert.equal(session.agentId, "flow_agent");
    assert.match(session.cdpUrl, /\/sessions\/slt_/);
    await page.goto(`${server.base}/gov/form`);
    await page.fill("#q", "hello");
    assert.equal(await page.title(), "Form");
  } finally {
    await close();
  }
  await assert.rejects(chromium.connectOverCDP(session.cdpUrl, { timeout: 5000 }), /Unexpected status 404/);
});

test("refusals are StatelockClientError with the status", async () => {
  await assert.rejects(createSessionUrl({ serverUrl: server.base, apiKey: "slk_wrong" }), (error) => {
    assert.ok(error instanceof StatelockClientError);
    assert.equal(error.status, 401);
    return true;
  });
  await assert.rejects(createSessionUrl({ serverUrl: server.base, saveSession: true }), StatelockClientError);
});

test("guard turns a violation into StatelockPolicyViolationError", async () => {
  const { session, page, close } = await openSession(server, "finance_reconciliation_agent");
  try {
    await assert.rejects(
      session.guard(async () => {
        await page.goto(`${server.base}/demo/finance?scenario=mismatch`);
        await page.click("text=Mark as Paid", { timeout: 10_000 });
        await page.title();
      }),
      (error) => {
        assert.ok(error instanceof StatelockPolicyViolationError, String(error));
        assert.equal(error.rule, "assert_field_equal");
        assert.equal(error.sessionId, session.sessionId);
        assert.equal(error.agentId, "finance_reconciliation_agent");
        return true;
      },
    );
    const violation = await session.violation(); // the same record, asked afterwards
    assert.ok(violation instanceof StatelockPolicyViolationError);
    assert.equal(violation.rule, "assert_field_equal");
  } finally {
    await close();
  }
});

test("guard lets other errors through", async () => {
  const { session, page, close } = await openSession(server);
  try {
    await page.goto(`${server.base}/gov/form`);
    await assert.rejects(session.guard(() => page.click("#missing", { timeout: 500 })), (error) => {
      assert.ok(!(error instanceof StatelockPolicyViolationError));
      return true;
    });
  } finally {
    await close();
  }
});

test("credential injection: the placeholder logs in, the agent never sees the password", async () => {
  const { page, close } = await openSession(server, "secret_agent");
  try {
    await page.goto(`${server.base}/gov/signin`);
    await page.fill("#username", "editor");
    await page.fill("#password", secret("portal_password"));
    assert.equal(await page.inputValue("#password"), "[SECRET]");
    await page.click("#submit");
    await page.waitForLoadState();
    assert.equal(await page.title(), "Welcome");
  } finally {
    await close();
  }
});

test("saved sessions: save at a clean end, list, delete", async () => {
  const options = { serverUrl: server.base, apiKey: key("flow_agent") };
  const first = await createSessionUrl({ ...options, savedSession: "js-login", saveSession: true });
  assert.equal(first.savedSession, "js-login");
  const browser = await chromium.connectOverCDP(first.cdpUrl);
  try {
    await browser.contexts()[0].pages()[0].goto(`${server.base}/gov/cookies`);
  } finally {
    await browser.close();
  }
  // Saved after the session closes (Chromium's teardown): allow for a busy machine (test files run in parallel).
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline && !(await listSavedSessions(options)).includes("js-login")) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.deepEqual(await listSavedSessions(options), ["js-login"]);
  assert.equal(await deleteSavedSession("js-login", options), true);
  assert.equal(await deleteSavedSession("js-login", options), false);
});

test("statelockFetch: governed HTTP in the page, with the browser's session", async () => {
  const { page, close } = await openSession(server, "request_agent");
  try {
    await page.goto(`${server.base}/gov/cookies`);
    const cdp = await page.context().newCDPSession(page);
    const me = await statelockFetch(cdp, `${server.base}/gov/me`);
    assert.ok(me instanceof StatelockResponse);
    assert.equal(me.status(), 200);
    assert.equal(me.ok(), true);
    assert.equal(me.url(), `${server.base}/gov/me`);
    assert.match(me.headers()["content-type"], /json/);
    assert.ok(me.headersArray().some(({ name }) => name.toLowerCase() === "content-type"));
    assert.deepEqual(await me.json(), { user: "signed-in" });
    const echoed = await statelockFetch(cdp, `${server.base}/gov/echo`, {
      method: "POST",
      headers: { "X-Api-Token": secret("api_token") },
      data: { a: 1 },
    });
    assert.equal((await echoed.json()).token_ok, true);
    await assert.rejects(statelockFetch(cdp, `${server.base}/gov/form`), /no request_access rule/);
  } finally {
    await close();
  }
});

test("an unreachable server is a StatelockClientError", async () => {
  await assert.rejects(
    createSessionUrl({ serverUrl: "http://127.0.0.1:9", apiKey: "k" }),
    (error) => error instanceof StatelockClientError && /could not reach Statelock/.test(error.message),
  );
});

test("a refused violation lookup is an error, not 'no violation'", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("flow_agent") });
  assert.equal(await session.violation(), null); // 404: none
  await assert.rejects(session.violation("slk_wrong"), (error) => {
    assert.ok(error instanceof StatelockClientError);
    assert.equal(error.status, 401);
    return true;
  });
  // The guard's watcher reports a wrong key instead of watching nothing.
  const options = { serverUrl: server.base, sessionId: session.sessionId, apiKey: "slk_wrong", watchIntervalMs: 50 };
  await assert.rejects(guard(() => new Promise((resolve) => setTimeout(resolve, 1000)), options), StatelockClientError);
});
