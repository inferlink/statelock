// Playwright through Statelock: install(), connectPlaywright() and the session's file methods.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { chromium } from "playwright-core";

import {
  StatelockDownloadError,
  StatelockPolicyViolationError,
  StatelockRequestError,
  StatelockResponse,
  uploadFiles,
} from "../dist/index.js";
import { StatelockDownload, connectPlaywright, install, statelockSession, uninstall } from "../dist/playwright.js";
import { key, openSession, startServer } from "./helpers.mjs";

let server;
before(async () => { server = await startServer(); });
after(async () => { uninstall(); await server.stop(); });

const pdf = (name) => ({ name, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1") });
const REPORT = Buffer.from("%PDF-1.4 quarterly report ".repeat(200)); // tests/browser_support.py FILES

async function until(condition, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

test("Playwright's own setInputFiles uploads through Statelock", async () => {
  const { session, page, close } = await openSession(server, "flow_agent", { install });
  try {
    assert.deepEqual(await statelockSession(page), { session_id: session.sessionId, agent_id: "flow_agent" });
    await page.goto(`${server.base}/gov/upload`);
    await page.setInputFiles("#file", pdf("a.pdf"), { timeout: 5000 });
    assert.equal(await page.innerText("#picked"), "picked:1");
    const dir = await mkdtemp(join(tmpdir(), "slt-"));
    await writeFile(join(dir, "b.txt"), "b");
    await writeFile(join(dir, "c.txt"), "c");
    await page.locator("#file").setInputFiles([join(dir, "b.txt"), join(dir, "c.txt")]);
    assert.equal(await page.innerText("#picked"), "picked:2");
    const handle = await page.$("#file");
    await handle.setInputFiles(pdf("d.pdf"));
    assert.equal(await page.innerText("#picked"), "picked:1");
    const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.click("#file")]);
    await chooser.setFiles(pdf("e.pdf"));
    assert.equal(await page.innerText("#picked"), "picked:1");
    // A page.on listener receives a patched FileChooser too.
    const listened = new Promise((resolve) => page.once("filechooser", resolve));
    await page.click("#file");
    await (await listened).setFiles(pdf("f.pdf"));
    assert.equal(await page.innerText("#picked"), "picked:1");
  } finally {
    await close();
  }
  const sessionDir = join(server.root, "artifacts", "sessions", session.sessionId);
  const uploads = [];
  for (const name of (await readdir(sessionDir)).filter((n) => n.startsWith("action-"))) {
    const record = JSON.parse(await readFile(join(sessionDir, name, "context.json"), "utf8"));
    if (record.context.method === "DOM.setFileInputFiles") uploads.push(record.context.params.statelock_uploads.length);
  }
  assert.deepEqual(uploads, [1, 2, 1, 1, 1]); // each upload governed and recorded
});

test("setInputFiles refuses options it would otherwise drop", async () => {
  const { page, close } = await openSession(server, "flow_agent", { install });
  try {
    await page.goto(`${server.base}/gov/upload`);
    await assert.rejects(page.setInputFiles("#file", pdf("a.pdf"), { force: true }), /unexpected options force/);
    await assert.rejects(page.locator("#file").setInputFiles(pdf("a.pdf"), { strict: false }), /strict: false/);
    // timeout bounds the wait for the input.
    await assert.rejects(page.setInputFiles("#absent", pdf("a.pdf"), { timeout: 300 }), /Timeout 300ms/);
    assert.equal(await page.innerText("#picked"), "none"); // nothing was set
  } finally {
    await close();
  }
});

test("page.waitForEvent('download') returns the download Statelock checked", async () => {
  const { page, close } = await openSession(server, "flow_agent", { install });
  try {
    await page.goto(`${server.base}/gov/docs`);
    const [download] = await Promise.all([page.waitForEvent("download"), page.click("#pdf")]);
    assert.equal(download.suggestedFilename(), "report.pdf");
    assert.equal(await download.failure(), null);
    const target = join(await mkdtemp(join(tmpdir(), "slt-")), "report.pdf");
    await download.saveAs(target);
    const bytes = await readFile(target);
    assert.ok(bytes.toString().startsWith("%PDF-1.4 quarterly report"));
    assert.deepEqual(await readFile(await download.path()), bytes);
    assert.equal(download.sha256.length, 64);
  } finally {
    await close();
  }
});

test("waitForEvent('download') honours the context's default timeout", async () => {
  const { page, close } = await openSession(server, "flow_agent", { install });
  try {
    await page.goto(`${server.base}/gov/docs`);
    page.context().setDefaultTimeout(1500);
    const started = Date.now();
    await assert.rejects(
      Promise.all([page.waitForEvent("download", { predicate: () => false }), page.click("#pdf")]),
      (error) => error instanceof StatelockDownloadError && /no download completed/.test(error.message),
    );
    const waited = Date.now() - started;
    assert.ok(waited >= 1400 && waited < 15_000, `waited ${waited} ms`); // not the 30 s default
  } finally {
    await close();
  }
});

test("a blocked download rejects with StatelockPolicyViolationError", async () => {
  const { session, page, close } = await openSession(server, "flow_agent", { install });
  try {
    await page.goto(`${server.base}/gov/docs`);
    // The session ends; guard() reports why (the download itself may see the closed session first).
    await assert.rejects(
      session.guard(() => Promise.all([page.waitForEvent("download"), page.click("#exe")])),
      (error) => {
        assert.ok(error instanceof StatelockPolicyViolationError, String(error));
        assert.equal(error.rule, "restrict_downloads");
        return true;
      },
    );
  } finally {
    await close();
  }
});

test("plain browsers are untouched", async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  try {
    await install(browser);
    const page = await browser.newPage();
    await page.setContent("<input id=f type=file><p id=n></p>");
    await page.evaluate(() => { f.onchange = () => { n.textContent = String(f.files.length); }; });
    await page.setInputFiles("#f", { name: "a.txt", mimeType: "text/plain", buffer: Buffer.from("a") });
    assert.equal(await statelockSession(page), null);
    assert.equal(await page.innerText("#n"), "1");
    // Straight to the site, not through Statelock: Playwright's own request and download event.
    const response = await page.request.get(`${server.base}/gov/form`);
    assert.ok(!(response instanceof StatelockResponse) && response.ok());
    assert.match(await response.text(), /Form/);
    const downloads = [];
    page.on("download", (download) => downloads.push(download));
    await page.goto(`${server.base}/gov/docs`);
    await page.click("#pdf");
    await until(() => downloads.length);
    assert.ok(!(downloads[0] instanceof StatelockDownload));
    assert.equal(downloads[0].suggestedFilename(), "report.pdf");
  } finally {
    await browser.close();
  }
});

test("page.on('download') gets the download Statelock checked; once() and off() work", async () => {
  const { page, close } = await openSession(server, "flow_agent", { install });
  try {
    await page.goto(`${server.base}/gov/docs`);
    const every = [];
    const first = [];
    const read = [];
    const onDownload = (download) => every.push(download);
    page.on("download", onDownload);
    page.once("download", (download) => first.push(download));
    page.on("download", async (download) => read.push(await download.readBytes()));
    for (const count of [1, 2]) {
      await page.click("#pdf");
      await until(() => read.length === count);
    }
    page.off("download", onDownload);
    await page.click("#pdf");
    await until(() => read.length === 3);
    assert.equal(every.length, 2); // removed before the third download
    assert.equal(first.length, 1); // once
    assert.deepEqual(read, [REPORT, REPORT, REPORT]);
    assert.ok(every[0] instanceof StatelockDownload);
    assert.equal(every[0].suggestedFilename(), "report.pdf");
    assert.equal(every[0].sha256, createHash("sha256").update(REPORT).digest("hex"));
    assert.notEqual(every[0].info.guid, every[1].info.guid);
    const target = join(await mkdtemp(join(tmpdir(), "slt-")), "on.pdf");
    await every[0].saveAs(target);
    assert.deepEqual(await readFile(target), REPORT);
  } finally {
    await close();
  }
});

test("page.on('download') goes to the page that started the download", async () => {
  const { page, close } = await openSession(server, "flow_agent", { install });
  try {
    const other = await page.context().newPage();
    const got = new Map([[page, []], [other, []]]);
    for (const each of [page, other]) {
      await each.goto(`${server.base}/gov/docs`);
      each.on("download", (download) => got.get(each).push(download.page() === each && download.suggestedFilename()));
    }
    await other.click("#pdf");
    await until(() => got.get(other).length);
    await page.click("#pdf");
    await until(() => got.get(page).length);
    await new Promise((resolve) => setTimeout(resolve, 600)); // nothing more arrives
    assert.deepEqual(got.get(page), ["report.pdf"]);
    assert.deepEqual(got.get(other), ["report.pdf"]);
  } finally {
    await close();
  }
});

test("page.request and context.request go through Statelock", async () => {
  const { session, page, close } = await openSession(server, "request_agent", { install });
  try {
    await page.goto(`${server.base}/gov/cookies`); // the site sets its HttpOnly session cookie
    const me = await page.request.get(`${server.base}/gov/me`);
    assert.ok(me instanceof StatelockResponse);
    assert.equal(me.status(), 200);
    assert.deepEqual(await me.json(), { user: "signed-in" });
    const json = await (await page.request.post(`${server.base}/gov/echo`, { data: { a: 1 } })).json();
    assert.deepEqual([JSON.parse(json.body), json.content_type], [{ a: 1 }, "application/json"]);
    const viaContext = await page.context().request.fetch(`${server.base}/gov/echo`, { method: "POST", form: { a: "1 2" } });
    const form = await viaContext.json();
    assert.deepEqual([form.body, form.content_type], ["a=1+2", "application/x-www-form-urlencoded"]);
    // Declined: the session continues.
    await assert.rejects(page.request.delete(`${server.base}/gov/me`), (error) => {
      assert.ok(error instanceof StatelockRequestError, String(error));
      assert.match(error.message, /no request_access rule/);
      return true;
    });
    await assert.rejects(
      page.request.get(`${server.base}/gov/me`, { maxRedirects: 2 }),
      /not supported through Statelock: maxRedirects/,
    );
    assert.equal(await page.title(), "Cookies");
  } finally {
    await close();
  }
  const fetches = [];
  const sessionDir = join(server.root, "artifacts", "sessions", session.sessionId);
  for (const name of (await readdir(sessionDir)).filter((n) => n.startsWith("action-"))) {
    const record = JSON.parse(await readFile(join(sessionDir, name, "context.json"), "utf8"));
    if (record.context.method === "Statelock.fetch") fetches.push(record);
  }
  assert.equal(fetches.length, 4); // each request governed and recorded, the declined one too
});

test("uploads return the proxy's fields, mimeType included", async () => {
  const { page, close } = await openSession(server);
  try {
    const cdp = await page.context().newCDPSession(page);
    const [stored] = await uploadFiles(cdp, pdf("g.pdf"));
    assert.deepEqual(Object.keys(stored).sort(), ["mimeType", "name", "path", "sha256", "size"]);
    assert.equal(stored.mimeType, "application/pdf");
    const dir = await mkdtemp(join(tmpdir(), "slt-"));
    await writeFile(join(dir, "paper.tex"), "\\documentclass{article}");
    const [tex] = await uploadFiles(cdp, join(dir, "paper.tex"));
    assert.equal(tex.mimeType, "application/x-tex"); // the same table as the Python SDK
  } finally {
    await close();
  }
});

test("a failed Statelock.session check is not remembered as a plain browser", async () => {
  const { session, browser, close } = await openSession(server);
  try {
    const context = browser.contexts()[0];
    const page = await context.newPage();
    // A closed page cannot answer: the error says nothing about the browser.
    const closed = await context.newPage();
    await closed.close();
    await assert.rejects(statelockSession(closed));
    assert.deepEqual(await statelockSession(page), { session_id: session.sessionId, agent_id: "flow_agent" });
  } finally {
    await close();
  }
});

test("connectPlaywright: a governed session from STATELOCK_URL and STATELOCK_API_KEY", async () => {
  const saved = { url: process.env.STATELOCK_URL, key: process.env.STATELOCK_API_KEY };
  process.env.STATELOCK_URL = server.base;
  process.env.STATELOCK_API_KEY = key("finance_reconciliation_agent");
  let governed;
  try {
    governed = await connectPlaywright(chromium, { connectOptions: { timeout: 30_000 } });
  } finally {
    for (const [name, value] of [["STATELOCK_URL", saved.url], ["STATELOCK_API_KEY", saved.key]]) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
  try {
    assert.equal(governed.session.agentId, "finance_reconciliation_agent");
    assert.deepEqual(await statelockSession(governed.page), {
      session_id: governed.session.sessionId,
      agent_id: "finance_reconciliation_agent",
    });
    await assert.rejects(
      governed.guard(async () => {
        await governed.page.goto(`${server.base}/demo/finance?scenario=mismatch`);
        await governed.page.click("text=Mark as Paid", { timeout: 10_000 });
        await governed.page.title();
      }),
      (error) => {
        assert.ok(error instanceof StatelockPolicyViolationError, String(error));
        assert.equal(error.rule, "assert_field_equal");
        return true;
      },
    );
  } finally {
    await governed.close().catch(() => undefined);
  }
  assert.equal(governed.browser.isConnected(), false);
});

test("the session's setInputFiles and expectDownload work without install()", async () => {
  uninstall();
  const governed = await connectPlaywright(chromium, { serverUrl: server.base, apiKey: key("flow_agent") });
  try {
    await governed.guard(async () => {
      const { page } = governed;
      await page.goto(`${server.base}/gov/upload`);
      const [stored] = await governed.setInputFiles("#file", pdf("a.pdf"), { timeout: 5000 });
      assert.equal(stored.name, "a.pdf");
      assert.equal(await page.innerText("#picked"), "picked:1");
      const other = await page.context().newPage();
      await other.goto(`${server.base}/gov/docs`);
      const [download] = await Promise.all([
        governed.expectDownload({ page: other, predicate: (d) => d.suggestedFilename() === "report.pdf", timeout: 10_000 }),
        other.click("#pdf"),
      ]);
      assert.ok((await download.readBytes()).toString().startsWith("%PDF-1.4 quarterly report"));
    });
  } finally {
    await governed.close();
  }
});
