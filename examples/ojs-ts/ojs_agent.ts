// SPDX-License-Identifier: Apache-2.0
/**
 * The OJS screening agent in TypeScript, on Stagehand v3, through Statelock.
 *
 * A shorter port of examples/ojs/ojs_agent.py. It logs in (without the password),
 * lists the active submissions, and for each one downloads the manuscript, reads
 * the comments to the editor, and runs basic checks. Results go to results.json.
 *
 * The agent is ordinary Stagehand code. The Statelock parts are marked "Statelock":
 * a session URL (with a saved login), guard(), the password placeholder, and the
 * download helpers (Stagehand v3 has no download API of its own).
 *
 * Environment:
 *   STATELOCK_URL, STATELOCK_API_KEY   the Statelock server and the agent's key
 *   OJS_BASE_URL          http://127.0.0.1:8081/index.php/journal (the mock: examples/ojs/mock_ojs.py)
 *   OJS_USERNAME          the editor's user name
 *   OJS_PASSWORD_SECRET   the secret Statelock types as the password (default ojs_password)
 *   OJS_SAVED_SESSION     the saved login (default ojs-editor-ts; empty: log in every run)
 *   OJS_OUTPUT_DIR        where manuscripts and results.json go (default ./ojs-output)
 *   OJS_DEMO_PROHIBITED_CLICK  demo only: click this workflow button (e.g. "Send to Review") on the first paper
 */

import { mkdir, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";

import { Stagehand } from "@browserbasehq/stagehand";
import {
  StatelockPolicyViolationError,
  cdpSender,
  createSessionUrl,
  knownDownloads,
  readDownload,
  secret,
  waitForDownload,
} from "@statelock/client";

/** An XPath string literal for any text (a quote in it would end a '...' literal). */
function xpathLiteral(text: string): string {
  if (!text.includes("'")) return `'${text}'`;
  if (!text.includes('"')) return `"${text}"`;
  return `concat('${text.split("'").join(`', "'", '`)}')`;
}

type Page = ReturnType<Stagehand["context"]["pages"]>[number];

interface Screening {
  paperId: number;
  manuscript: { name: string; size: number; sha256: string; isPdf: boolean } | null;
  comments: string;
  checks: Record<string, boolean>;
}

const settings = {
  baseUrl: (process.env.OJS_BASE_URL ?? "http://127.0.0.1:8081/index.php/journal").replace(/\/+$/, ""),
  username: process.env.OJS_USERNAME ?? "editor",
  passwordSecret: process.env.OJS_PASSWORD_SECRET ?? "ojs_password",
  savedSession: process.env.OJS_SAVED_SESSION ?? "ojs-editor-ts",
  outputDir: process.env.OJS_OUTPUT_DIR ?? "ojs-output",
  prohibitedClick: process.env.OJS_DEMO_PROHIBITED_CLICK || null,
};
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "host.docker.internal", "ojs-mock"]);
const url = (path: string) => `${settings.baseUrl}/${path.replace(/^\/+/, "")}`;

async function login(page: Page): Promise<void> {
  await page.goto(url("submissions"), { waitUntil: "networkidle" });
  if (!page.url().includes("/login")) {
    console.log("Already logged in (saved session)");
    return;
  }
  await page.locator("#username").fill(settings.username);
  // Statelock: a placeholder, not the password. Statelock types the secret into the field.
  await page.locator("input[type=password]").fill(secret(settings.passwordSecret));
  await page.locator("form#login button[type=submit]").click();
  await page.waitForLoadState("networkidle");
  if (page.url().includes("/login")) throw new Error("Login failed: still on the login page");
  console.log(`Logged in as ${settings.username}`);
}

async function activeSubmissionIds(page: Page): Promise<number[]> {
  await page.goto(url("submissions"), { waitUntil: "networkidle" });
  await page.locator("xpath=//*[@role='tab'][contains(normalize-space(), 'All Active')]").click();
  // Only visible links: OJS keeps the other tabs' lists in the DOM.
  const hrefs = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLAnchorElement>('a[href*="/workflow/"]')]
      .filter((el) => el.offsetParent !== null)
      .map((el) => el.getAttribute("href") ?? ""),
  );
  const ids = [...new Set(hrefs.map((href) => /\/workflow\/(?:access|index)\/(\d+)/.exec(href)?.[1]).filter(Boolean))];
  return ids.map(Number);
}

async function openSubmission(page: Page, paperId: number): Promise<void> {
  await page.goto(url(`workflow/access/${paperId}`), { waitUntil: "networkidle" });
}

async function downloadManuscript(page: Page, screening: Screening): Promise<void> {
  const index = await page.evaluate(() =>
    [...document.querySelectorAll("a.pkp_linkaction_downloadFile")].findIndex((el) =>
      ((el.closest("li, tr") ?? el).textContent ?? "").includes("Article"),
    ),
  );
  if (index < 0) return;
  // Statelock: the proxy governs the download; the agent reads it back over CDP.
  const cdp = cdpSender(page);
  const known = await knownDownloads(cdp);
  await page.locator("a.pkp_linkaction_downloadFile").nth(index).click();
  const download = await waitForDownload(cdp, known);
  const data = await readDownload(cdp, download);
  // The name comes from the site: only its last part, so the file stays in outputDir.
  await writeFile(join(settings.outputDir, `paper-${screening.paperId}-${basename(download.name)}`), data);
  screening.manuscript = {
    name: download.name,
    size: data.length,
    sha256: download.sha256,
    isPdf: data.subarray(0, 5).toString("latin1") === "%PDF-", // a .tex or .docx manuscript fails
  };
}

async function readComments(page: Page, screening: Screening): Promise<void> {
  await page.locator("#editor-comments").click();
  await page.waitForSelector(".pkpModal:not([hidden])", { timeout: 5000 });
  screening.comments = await page.evaluate(() =>
    [...document.querySelectorAll(".pkpModal:not([hidden]) .noteContent")]
      .map((el) => (el.textContent ?? "").trim())
      .join("\n\n"),
  );
  await page.locator(".pkpModal:not([hidden]) .pkpModal__close").click();
}

async function screen(page: Page, paperId: number): Promise<Screening> {
  const screening: Screening = { paperId, manuscript: null, comments: "", checks: {} };
  await openSubmission(page, paperId);
  await downloadManuscript(page, screening);
  await readComments(page, screening);
  screening.checks = {
    has_manuscript_pdf: Boolean(screening.manuscript?.isPdf),
    has_comments_to_editor: Boolean(screening.comments.trim()),
  };
  console.log(`Paper ${paperId} checks: ${JSON.stringify(screening.checks)}`);
  return screening;
}

async function run(stagehand: Stagehand): Promise<Screening[]> {
  const page = stagehand.context.pages()[0];
  await mkdir(settings.outputDir, { recursive: true });
  await login(page);
  const ids = await activeSubmissionIds(page);
  console.log(`Active submissions: ${ids.join(", ")}`);
  if (!ids.length) throw new Error("No active submissions found");
  if (settings.prohibitedClick) {
    await openSubmission(page, ids[0]);
    console.warn(`Demo: clicking "${settings.prohibitedClick}", a button the policy prohibits`);
    await stagehand.act({
      selector: `xpath=//a[normalize-space()=${xpathLiteral(settings.prohibitedClick)}]`,
      description: settings.prohibitedClick,
      method: "click",
      arguments: [],
    });
    // Statelock ends the session; guard()'s watch (every second) turns that into
    // StatelockPolicyViolationError, which ends this wait. It is not a fixed delay.
    await page.waitForTimeout(15_000);
    throw new Error("NOT blocked: the prohibited click went through");
  }
  const screenings: Screening[] = [];
  for (const id of ids) screenings.push(await screen(page, id));
  await writeFile(join(settings.outputDir, "results.json"), JSON.stringify(screenings, null, 2));
  return screenings;
}

async function main(): Promise<number> {
  const host = new URL(settings.baseUrl).hostname;
  if (!LOCAL_HOSTS.has(host) && process.env.OJS_ALLOW_REMOTE !== "1") {
    console.error(`OJS_BASE_URL points at ${host}; set OJS_ALLOW_REMOTE=1 to use a real journal`);
    return 1;
  }
  // Statelock: a one-time browser URL (STATELOCK_URL, STATELOCK_API_KEY), with the saved login.
  const session = await createSessionUrl({
    savedSession: settings.savedSession || undefined,
    saveSession: Boolean(settings.savedSession),
  });
  const stagehand = new Stagehand({
    env: "LOCAL",
    verbose: 0,
    disablePino: true,
    localBrowserLaunchOptions: { cdpUrl: session.wsUrl },
  });
  try {
    await stagehand.init();
    await session.guard(() => run(stagehand)); // Statelock (optional): a violation throws StatelockPolicyViolationError
    return 0;
  } catch (error) {
    if (error instanceof StatelockPolicyViolationError) {
      console.error(`Statelock stopped the agent: ${error.rule}: ${error.reason}`);
      return 2;
    }
    throw error;
  } finally {
    await stagehand.close().catch(() => undefined);
  }
}

process.exitCode = await main();
