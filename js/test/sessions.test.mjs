import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { chromium } from "playwright-core";

import {
  SessionUrlError,
  StatelockPolicyViolationError,
  createSessionUrl,
  decodeViolation,
  deleteSavedSession,
  fetchViolation,
  guard,
  listSavedSessions,
  secret,
  statelockFetch,
} from "../dist/index.js";
import { key, startServer } from "./helpers.mjs";

let server;
before(async () => { server = await startServer(); });
after(async () => { await server.stop(); });

test("a session URL opens a governed session, once", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("flow_agent") });
  assert.equal(session.agentId, "flow_agent");
  assert.match(session.cdpUrl, /\/sessions\/slt_/);
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  const page = browser.contexts()[0].pages()[0];
  await page.goto(`${server.base}/gov/form`);
  await page.fill("#q", "hello");
  assert.equal(await page.title(), "Form");
  await browser.close();
  await assert.rejects(chromium.connectOverCDP(session.cdpUrl, { timeout: 5000 }));
});

test("refusals are SessionUrlError with the status", async () => {
  await assert.rejects(createSessionUrl({ serverUrl: server.base, apiKey: "slk_wrong" }), (error) => {
    assert.ok(error instanceof SessionUrlError);
    assert.equal(error.status, 401);
    return true;
  });
  await assert.rejects(createSessionUrl({ serverUrl: server.base, saveSession: true }), SessionUrlError);
});

test("guard turns a violation into StatelockPolicyViolationError", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("finance_reconciliation_agent") });
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  const page = browser.contexts()[0].pages()[0];
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
  await browser.close().catch(() => undefined);
});

test("guard lets other errors through", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("flow_agent") });
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  const page = browser.contexts()[0].pages()[0];
  await page.goto(`${server.base}/gov/form`);
  await assert.rejects(session.guard(() => page.click("#missing", { timeout: 500 })), (error) => {
    assert.ok(!(error instanceof StatelockPolicyViolationError));
    return true;
  });
  await browser.close();
});

test("credential injection: the placeholder logs in, the agent never sees the password", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("secret_agent") });
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  const page = browser.contexts()[0].pages()[0];
  await page.goto(`${server.base}/gov/signin`);
  await page.fill("#username", "editor");
  await page.fill("#password", secret("portal_password"));
  assert.equal(await page.inputValue("#password"), "[SECRET]");
  await page.click("#submit");
  await page.waitForLoadState();
  assert.equal(await page.title(), "Welcome");
  await browser.close();
});

test("saved sessions: save at a clean end, list, delete", async () => {
  const options = { serverUrl: server.base, apiKey: key("flow_agent") };
  const first = await createSessionUrl({ ...options, savedSession: "js-login", saveSession: true });
  assert.equal(first.savedSession, "js-login");
  let browser = await chromium.connectOverCDP(first.cdpUrl);
  await browser.contexts()[0].pages()[0].goto(`${server.base}/gov/cookies`);
  await browser.close();
  // Saved after the session closes (Chromium's teardown): allow for a busy machine (test files run in parallel).
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline && !(await listSavedSessions(options)).includes("js-login")) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.deepEqual(await listSavedSessions(options), ["js-login"]);
  assert.equal(await deleteSavedSession("js-login", options), true);
  assert.equal(await deleteSavedSession("js-login", options), false);
});

test("decodeViolation reads the full and the compact form", () => {
  assert.deepEqual(decodeViolation('x STATELOCK_POLICY_VIOLATION {"rule":"r","reason":"a {b}"} y'), { rule: "r", reason: "a {b}" });
  assert.deepEqual(decodeViolation('STATELOCK_POLICY_VIOLATION {"t":"pre","r":"x","s":"id","n":3}'), {
    violation_type: "pre_condition", rule: "x", session_id: "id", sequence: 3,
  });
  assert.equal(decodeViolation("nothing"), null);
});

test("statelockFetch: governed HTTP in the page, with the browser's session", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("request_agent") });
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  const page = browser.contexts()[0].pages()[0];
  await page.goto(`${server.base}/gov/cookies`);
  const cdp = await page.context().newCDPSession(page);
  const me = await statelockFetch(cdp, `${server.base}/gov/me`);
  assert.deepEqual(me.json(), { user: "signed-in" });
  const echoed = await statelockFetch(cdp, `${server.base}/gov/echo`, {
    method: "POST",
    headers: { "X-Api-Token": secret("api_token") },
    data: { a: 1 },
  });
  assert.equal(echoed.json().token_ok, true);
  await assert.rejects(statelockFetch(cdp, `${server.base}/gov/form`), /no request_access rule/);
  await browser.close();
});

test("an unreachable server is a SessionUrlError", async () => {
  await assert.rejects(
    createSessionUrl({ serverUrl: "http://127.0.0.1:9", apiKey: "k" }),
    (error) => error instanceof SessionUrlError && /could not reach Statelock/.test(error.message),
  );
});

test("a refused violation lookup is an error, not 'no violation'", async () => {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key("flow_agent") });
  assert.equal(await fetchViolation(session.cdpUrl, session.sessionId, key("flow_agent")), null); // 404: none
  await assert.rejects(fetchViolation(session.cdpUrl, session.sessionId, "slk_wrong"), (error) => {
    assert.ok(error instanceof SessionUrlError);
    assert.equal(error.status, 401);
    return true;
  });
  // The guard's watcher reports a wrong key instead of watching nothing.
  const options = { serverUrl: server.base, sessionId: session.sessionId, apiKey: "slk_wrong", watchIntervalMs: 50 };
  await assert.rejects(guard(() => new Promise((resolve) => setTimeout(resolve, 1000)), options), SessionUrlError);
});
