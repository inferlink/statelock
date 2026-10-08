// SPDX-License-Identifier: Apache-2.0
/**
 * Make Playwright's own file APIs work through Statelock, with no other code changes.
 *
 *   import { install } from "@statelock/client/playwright";
 *   const browser = await chromium.connectOverCDP(session.cdpUrl);
 *   await install(browser);
 *
 * After install(), on browsers behind Statelock:
 * - setInputFiles (Page, Locator, ElementHandle) and FileChooser.setFiles upload the
 *   files through Statelock, as governed uploads;
 * - page.waitForEvent("download") resolves with the download Statelock checked, and
 *   it works like Playwright's Download (saveAs, path, suggestedFilename, url).
 *
 * FileChooser is not reachable before a page opens one, so install() hooks the page's
 * event emitter: each "filechooser" event's FileChooser class is patched before any
 * listener (page.on, page.waitForEvent) receives it.
 *
 * Other browsers are untouched: each is asked once whether it is a Statelock session.
 * install() patches Playwright's classes, so it needs one live browser to find them.
 * Not covered: page.on("download") listeners.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

import type { Browser, CDPSession, ElementHandle, Locator, Page } from "playwright-core";

import {
  type DownloadInfo,
  type UploadFile,
  type UploadedFile,
  knownDownloads,
  readDownload,
  statelockSession,
  uploadFiles,
  waitForDownload,
} from "./cdp.js";

const MARKER_ATTRIBUTE = "data-statelock-upload-target";
const DEFAULT_DOWNLOAD_TIMEOUT_MS = 30_000;

type AnyFunction = (...args: any[]) => any; // eslint-disable-line @typescript-eslint/no-explicit-any
const originals = new Map<object, Map<string, AnyFunction>>();
const governed = new WeakMap<object, Promise<{ session_id: string; agent_id: string } | null>>();

async function withCdp<T>(page: Page, fn: (cdp: CDPSession) => Promise<T>): Promise<T> {
  const cdp = await page.context().newCDPSession(page);
  try {
    return await fn(cdp);
  } finally {
    await cdp.detach().catch(() => undefined);
  }
}

/** {session_id, agent_id} if the page's browser is connected through Statelock, else null. */
export function pageSession(page: Page): Promise<{ session_id: string; agent_id: string } | null> {
  const key: object = page.context().browser() ?? page.context();
  let known = governed.get(key);
  if (!known) {
    const asked = withCdp(page, (cdp) => statelockSession(cdp));
    governed.set(key, asked);
    // Only an answer is kept: after an error (a closed page) the next call asks again.
    asked.catch(() => {
      if (governed.get(key) === asked) governed.delete(key);
    });
    known = asked;
  }
  return known;
}

/** Upload files through Statelock and set them on a file input of the page's main frame. */
export async function setInputFiles(
  page: Page,
  target: string | Locator | ElementHandle,
  files: UploadFile | UploadFile[],
): Promise<UploadedFile[]> {
  const element = typeof target === "string" ? page.locator(target) : target;
  const marker = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let marked = false;
  try {
    return await withCdp(page, async (cdp) => {
      const uploaded = await uploadFiles(cdp, files);
      await (element as Locator).evaluate(
        (el: Element, [name, value]: [string, string]) => el.setAttribute(name, value),
        [MARKER_ATTRIBUTE, marker] as [string, string],
      );
      marked = true;
      const document = (await cdp.send("DOM.getDocument", { depth: 0 })) as { root: { nodeId: number } };
      const found = (await cdp.send("DOM.querySelector", {
        nodeId: document.root.nodeId,
        selector: `[${MARKER_ATTRIBUTE}="${marker}"]`,
      })) as { nodeId?: number };
      if (!found.nodeId) throw new Error("setInputFiles: the file input must be in the page's main frame");
      await cdp.send("DOM.setFileInputFiles", { nodeId: found.nodeId, files: uploaded.map((file) => file.path) });
      return uploaded;
    });
  } finally {
    if (marked) {
      await (element as Locator)
        .evaluate((el: Element, name: string) => el.removeAttribute(name), MARKER_ATTRIBUTE)
        .catch(() => undefined);
    }
  }
}

/** A download that passed Statelock's checks, with Playwright's Download API. */
export class StatelockDownload {
  private savedPath: string | null = null;
  private savedDir: string | null = null;

  constructor(
    private readonly owner: Page,
    readonly info: DownloadInfo,
  ) {}

  page(): Page {
    return this.owner;
  }

  url(): string {
    return this.info.url;
  }

  suggestedFilename(): string {
    return this.info.name;
  }

  get sha256(): string {
    return this.info.sha256;
  }

  async failure(): Promise<string | null> {
    return null; // a download that failed or was blocked is never handed over
  }

  async cancel(): Promise<void> {} // already completed and checked

  /** The bytes, fetched from Statelock and checked against the recorded SHA-256. */
  async readBytes(): Promise<Buffer> {
    return withCdp(this.owner, (cdp) => readDownload(cdp, this.info));
  }

  async saveAs(path: string): Promise<void> {
    await writeFile(path, await this.readBytes());
  }

  /** A temporary copy on this machine (created once). */
  async path(): Promise<string> {
    if (!this.savedPath) {
      this.savedDir = await mkdtemp(join(tmpdir(), "statelock-"));
      // The name comes from the page: only its last part, so it cannot leave the folder.
      this.savedPath = join(this.savedDir, basename(this.info.name) || "download");
      await this.saveAs(this.savedPath);
    }
    return this.savedPath;
  }

  async delete(): Promise<void> {
    if (this.savedDir) await rm(this.savedDir, { recursive: true, force: true });
    this.savedPath = null;
    this.savedDir = null;
  }
}

type DownloadPredicate = (download: StatelockDownload) => boolean | Promise<boolean>;

/**
 * Wait for the next download the page starts (the first one ``predicate`` accepts),
 * after Statelock checked it. ``timeoutMs`` 0 waits without a limit, as in Playwright.
 *
 * Downloads Statelock already knows when the wait starts do not count: start the wait
 * before the click, as with Playwright (``Promise.all([page.waitForEvent("download"), click])``
 * starts both at once, and a download that completes within a few milliseconds of the
 * click can be missed).
 */
export async function waitForStatelockDownload(
  page: Page,
  timeoutMs = DEFAULT_DOWNLOAD_TIMEOUT_MS,
  predicate?: DownloadPredicate,
): Promise<StatelockDownload> {
  return withCdp(page, async (cdp) => {
    const known = await knownDownloads(cdp);
    const deadline = timeoutMs > 0 ? Date.now() + timeoutMs : Infinity;
    for (;;) {
      const info = await waitForDownload(cdp, known, Math.max(0, deadline - Date.now()));
      const download = new StatelockDownload(page, info);
      if (!predicate || (await predicate(download))) return download;
      known.add(info.guid);
    }
  });
}

function downloadWaitOptions(optionsOrPredicate: unknown): { timeoutMs: number; predicate?: DownloadPredicate } {
  if (typeof optionsOrPredicate === "function") {
    return { timeoutMs: DEFAULT_DOWNLOAD_TIMEOUT_MS, predicate: optionsOrPredicate as DownloadPredicate };
  }
  const options = (optionsOrPredicate ?? {}) as { timeout?: number; predicate?: DownloadPredicate };
  return { timeoutMs: options.timeout ?? DEFAULT_DOWNLOAD_TIMEOUT_MS, predicate: options.predicate };
}

function patch(prototype: object, name: string, make: (original: AnyFunction) => AnyFunction): void {
  let methods = originals.get(prototype);
  if (!methods) originals.set(prototype, (methods = new Map()));
  if (methods.has(name)) return;
  const original = (prototype as Record<string, AnyFunction>)[name];
  methods.set(name, original);
  (prototype as Record<string, AnyFunction>)[name] = make(original);
}

async function frameOwner(handle: ElementHandle): Promise<Page | null> {
  const frame = await handle.ownerFrame();
  return frame ? frame.page() : null;
}

function patchFileChooser(chooser: unknown): void {
  patch(Object.getPrototypeOf(chooser), "setFiles", (original) =>
    async function (this: { page(): Page; element(): ElementHandle }, files: UploadFile | UploadFile[], ...rest: unknown[]) {
      if (await pageSession(this.page())) {
        await setInputFiles(this.page(), this.element(), files);
        return;
      }
      return original.call(this, files, ...rest);
    },
  );
}

/** Route Playwright's upload and download APIs through Statelock (idempotent). */
export async function install(browser: Browser): Promise<void> {
  // A page to find Playwright's classes on; what install() had to open, it closes again.
  const existing = browser.contexts()[0];
  const context = existing ?? (await browser.newContext());
  const hadPage = context.pages().length > 0;
  const page = context.pages()[0] ?? (await context.newPage());
  const handle = await page.evaluateHandle(() => document.documentElement);
  const pageProto = Object.getPrototypeOf(page);
  const locatorProto = Object.getPrototypeOf(page.locator("html"));
  const handleProto = Object.getPrototypeOf(handle);
  await handle.dispose();
  if (!existing) await context.close();
  else if (!hadPage) await page.close();

  patch(pageProto, "setInputFiles", (original) =>
    async function (this: Page, selector: string, files: UploadFile | UploadFile[], ...rest: unknown[]) {
      if (await pageSession(this)) {
        await setInputFiles(this, selector, files);
        return;
      }
      return original.call(this, selector, files, ...rest);
    },
  );
  patch(locatorProto, "setInputFiles", (original) =>
    async function (this: Locator, files: UploadFile | UploadFile[], ...rest: unknown[]) {
      if (await pageSession(this.page())) {
        await setInputFiles(this.page(), this, files);
        return;
      }
      return original.call(this, files, ...rest);
    },
  );
  patch(handleProto, "setInputFiles", (original) =>
    async function (this: ElementHandle, files: UploadFile | UploadFile[], ...rest: unknown[]) {
      const owner = await frameOwner(this);
      if (owner && (await pageSession(owner))) {
        await setInputFiles(owner, this, files);
        return;
      }
      return original.call(this, files, ...rest);
    },
  );
  patch(pageProto, "waitForEvent", (original) =>
    async function (this: Page, event: string, optionsOrPredicate?: unknown, ...rest: unknown[]) {
      if (event === "download" && (await pageSession(this))) {
        const { timeoutMs, predicate } = downloadWaitOptions(optionsOrPredicate);
        return waitForStatelockDownload(this, timeoutMs, predicate);
      }
      return original.call(this, event, optionsOrPredicate, ...rest);
    },
  );
  patch(pageProto, "emit", (original) =>
    function (this: Page, event: string | symbol, ...args: unknown[]) {
      if (event === "filechooser" && args[0]) patchFileChooser(args[0]);
      return original.call(this, event, ...args);
    },
  );
}

/** Restore Playwright's own methods. */
export function uninstall(): void {
  for (const [prototype, methods] of originals) {
    for (const [name, original] of methods) (prototype as Record<string, AnyFunction>)[name] = original;
  }
  originals.clear();
}
