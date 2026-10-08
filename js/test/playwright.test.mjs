import assert from "node:assert/strict";
import { readFile, writeFile, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { chromium } from "playwright-core";

import { StatelockPolicyViolationError, createSessionUrl, uploadFiles } from "../dist/index.js";
import { install, pageSession, uninstall } from "../dist/playwright.js";
import { key, startServer } from "./helpers.mjs";

let server;
before(async () => { server = await startServer(); });
after(async () => { uninstall(); await server.stop(); });

async function open(agent = "flow_agent") {
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key(agent) });
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  await install(browser);
  return { session, browser, page: browser.contexts()[0].pages()[0] };
}

test("Playwright's own setInputFiles uploads through Statelock", async () => {
  const { session, browser, page } = await open();
  assert.deepEqual(await pageSession(page), { session_id: session.sessionId, agent_id: "flow_agent" });
  await page.goto(`${server.base}/gov/upload`);
  await page.setInputFiles("#file", { name: "a.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1") });
  assert.equal(await page.innerText("#picked"), "picked:1");
  const dir = await mkdtemp(join(tmpdir(), "slt-"));
  await writeFile(join(dir, "b.txt"), "b");
  await writeFile(join(dir, "c.txt"), "c");
  await page.locator("#file").setInputFiles([join(dir, "b.txt"), join(dir, "c.txt")]);
  assert.equal(await page.innerText("#picked"), "picked:2");
  const handle = await page.$("#file");
  await handle.setInputFiles({ name: "d.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1") });
  assert.equal(await page.innerText("#picked"), "picked:1");
  const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.click("#file")]);
  await chooser.setFiles({ name: "e.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1") });
  assert.equal(await page.innerText("#picked"), "picked:1");
  // A page.on listener receives a patched FileChooser too.
  const listened = new Promise((resolve) => page.once("filechooser", resolve));
  await page.click("#file");
  await (await listened).setFiles({ name: "f.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1") });
  assert.equal(await page.innerText("#picked"), "picked:1");
  await browser.close();
  const { readdir } = await import("node:fs/promises");
  const dir2 = join(server.root, "artifacts", "sessions", session.sessionId);
  const uploads = [];
  for (const name of (await readdir(dir2)).filter((n) => n.startsWith("action-"))) {
    const record = JSON.parse(await readFile(join(dir2, name, "context.json"), "utf8"));
    if (record.context.method === "DOM.setFileInputFiles") uploads.push(record.context.params.statelock_uploads.length);
  }
  assert.deepEqual(uploads, [1, 2, 1, 1, 1]); // each upload governed and recorded
});

test("page.waitForEvent('download') returns the download Statelock checked", async () => {
  const { browser, page } = await open();
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
  await browser.close();
});

test("a blocked download rejects with StatelockPolicyViolationError", async () => {
  const { session, browser, page } = await open();
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
  await browser.close().catch(() => undefined);
});

test("plain browsers are untouched", async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  try {
    await install(browser);
    const page = await browser.newPage();
    await page.setContent("<input id=f type=file><p id=n></p>");
    await page.evaluate(() => { f.onchange = () => { n.textContent = String(f.files.length); }; });
    await page.setInputFiles("#f", { name: "a.txt", mimeType: "text/plain", buffer: Buffer.from("a") });
    assert.equal(await pageSession(page), null);
    assert.equal(await page.innerText("#n"), "1");
  } finally {
    await browser.close();
  }
});

test("uploads return the proxy's fields, mimeType included", async () => {
  const { browser, page } = await open();
  const cdp = await page.context().newCDPSession(page);
  const [stored] = await uploadFiles(cdp, { name: "g.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1") });
  assert.deepEqual(Object.keys(stored).sort(), ["mimeType", "name", "path", "sha256", "size"]);
  assert.equal(stored.mimeType, "application/pdf");
  await browser.close();
});

test("a failed Statelock.session check is not remembered as a plain browser", async () => {
  const { session, browser } = await open();
  const context = browser.contexts()[0];
  const page = await context.newPage();
  // A closed page cannot answer: the error says nothing about the browser.
  const closed = await context.newPage();
  await closed.close();
  await assert.rejects(pageSession(closed));
  assert.deepEqual(await pageSession(page), { session_id: session.sessionId, agent_id: "flow_agent" });
  await browser.close();
});
