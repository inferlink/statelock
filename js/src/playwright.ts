// SPDX-License-Identifier: Apache-2.0
/**
 * Playwright through Statelock: connectPlaywright() opens a governed session, and
 * install() makes Playwright's own file APIs work through Statelock, with no other
 * code changes.
 *
 *   import { chromium } from "playwright-core";
 *   import { connectPlaywright, install } from "@statelock/client/playwright";
 *   const governed = await connectPlaywright(chromium);   // STATELOCK_URL, STATELOCK_API_KEY
 *   await install(governed.browser);
 *   await governed.guard(() => governed.page.goto(url));
 *   await governed.close();
 *
 * After install(), on browsers behind Statelock:
 * - setInputFiles (Page, Locator, ElementHandle) and FileChooser.setFiles upload the
 *   files through Statelock, as governed uploads;
 * - page.waitForEvent("download") resolves with the download Statelock checked, and
 *   it works like Playwright's Download (saveAs, path, suggestedFilename, url). Its
 *   default timeout is the page's (or context's) setDefaultTimeout, else 30 s.
 * Without install(), the session from connectPlaywright has the same two:
 * governed.setInputFiles(target, files) and governed.expectDownload().
 *
 * FileChooser is not reachable before a page opens one, so install() hooks the page's
 * event emitter: each "filechooser" event's FileChooser class is patched before any
 * listener (page.on, page.waitForEvent) receives it.
 *
 * Other browsers are untouched: each is asked once whether it is a Statelock session.
 * install() patches Playwright's classes, so it needs one live browser to find them.
 * Not covered: page.on("download") listeners.
 */

import { mkdtemp, open, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

import type {
  Browser,
  BrowserContext,
  BrowserType,
  CDPSession,
  ConnectOverCDPOptions,
  ElementHandle,
  Locator,
  Page,
} from "playwright-core";

import {
  DEFAULT_DOWNLOAD_TIMEOUT_MS,
  type DownloadInfo,
  type StatelockSessionInfo,
  type UploadFile,
  type UploadedFile,
  knownDownloads,
  sessionInfo,
  streamDownload,
  uploadFiles,
  waitForDownload,
} from "./cdp.js";
import { type SessionUrl, type SessionUrlOptions, createSessionUrl } from "./sessions.js";
import { StatelockPolicyViolationError } from "./violations.js";

const MARKER_ATTRIBUTE = "data-statelock-upload-target";
const UPLOAD_OPTIONS = new Set(["timeout", "noWaitAfter", "strict"]);

type AnyFunction = (...args: any[]) => any;
const originals = new Map<object, Map<string, AnyFunction>>();
const governed = new WeakMap<object, Promise<StatelockSessionInfo | null>>();
/** Timeouts set with Page/BrowserContext.setDefaultTimeout while installed (Playwright has no getter). */
const defaultTimeouts = new WeakMap<object, number>();

async function withCdp<T>(page: Page, fn: (cdp: CDPSession) => Promise<T>): Promise<T> {
  const cdp = await page.context().newCDPSession(page);
  try {
    return await fn(cdp);
  } finally {
    await cdp.detach().catch(() => undefined);
  }
}

/** {session_id, agent_id} if the page's browser is connected through Statelock, else null. */
export function statelockSession(page: Page): Promise<StatelockSessionInfo | null> {
  const key: object = page.context().browser() ?? page.context();
  let known = governed.get(key);
  if (!known) {
    const asked = withCdp(page, (cdp) => sessionInfo(cdp));
    governed.set(key, asked);
    // Only an answer is kept: after an error (a closed page) the next call asks again.
    asked.catch(() => {
      if (governed.get(key) === asked) governed.delete(key);
    });
    known = asked;
  }
  return known;
}

export interface SetInputFilesOptions {
  /** Milliseconds to wait for a selector or Locator target (Playwright's default otherwise). */
  timeout?: number;
  /** Accepted for Playwright compatibility; it has no effect there either. */
  noWaitAfter?: boolean;
  /** The input is always looked up strictly: false is refused rather than ignored. */
  strict?: boolean;
}

/** Check Playwright's setInputFiles / setFiles options for a governed upload (as the Python SDK does). */
function uploadOptions(options: unknown): SetInputFilesOptions {
  if (options === undefined || options === null) return {};
  if (typeof options !== "object") throw new TypeError("setInputFiles: options must be an object");
  const unknown = Object.keys(options).filter((name) => !UPLOAD_OPTIONS.has(name)).sort();
  if (unknown.length) throw new TypeError(`setInputFiles: unexpected options ${unknown.join(", ")}`);
  const checked = options as SetInputFilesOptions;
  if (checked.strict === false) throw new Error("setInputFiles: strict: false is not supported through Statelock");
  return checked;
}

/**
 * Upload files through Statelock and set them on a file input of the page's main frame.
 * ``options.timeout`` bounds the wait for a selector or Locator target.
 */
export async function setInputFiles(
  page: Page,
  target: string | Locator | ElementHandle,
  files: UploadFile | UploadFile[],
  options: SetInputFilesOptions = {},
): Promise<UploadedFile[]> {
  const { timeout } = uploadOptions(options);
  const element = typeof target === "string" ? page.locator(target) : target;
  const wait = timeout !== undefined && "waitFor" in element ? { timeout } : undefined;
  const marker = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let marked = false;
  try {
    return await withCdp(page, async (cdp) => {
      const uploaded = await uploadFiles(cdp, files);
      await (element as Locator).evaluate(
        (el: Element, [name, value]: [string, string]) => el.setAttribute(name, value),
        [MARKER_ATTRIBUTE, marker] as [string, string],
        wait,
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
    const chunks: Buffer[] = [];
    await withCdp(this.owner, (cdp) =>
      streamDownload(cdp, this.info, async (chunk) => {
        chunks.push(chunk);
      }),
    );
    return Buffer.concat(chunks);
  }

  /** Stream the file to path. A partial file is removed if the integrity check fails. */
  async saveAs(path: string): Promise<void> {
    const file = await open(path, "w");
    try {
      await withCdp(this.owner, (cdp) =>
        streamDownload(cdp, this.info, async (chunk) => {
          await file.write(chunk);
        }),
      );
    } catch (error) {
      await file.close();
      await rm(path, { force: true });
      throw error;
    }
    await file.close();
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

export interface ExpectDownloadOptions {
  /** The first download it accepts is the one returned. */
  predicate?: DownloadPredicate;
  /** Milliseconds; default: the page's (or context's) setDefaultTimeout after install(), else 30000; 0: no limit. */
  timeout?: number;
}

function defaultTimeout(page: Page): number {
  return defaultTimeouts.get(page) ?? defaultTimeouts.get(page.context()) ?? DEFAULT_DOWNLOAD_TIMEOUT_MS;
}

/**
 * Wait for the next download the page starts (the first one ``predicate`` accepts),
 * after Statelock checked it. A blocked download throws StatelockPolicyViolationError
 * (guard() completes it with the recorded violation).
 *
 * Downloads Statelock already knows when the wait starts do not count: start the wait
 * before the click, as with Playwright (``Promise.all([expectDownload(page), click])``
 * starts both at once, and a download that completes within a few milliseconds of the
 * click can be missed).
 */
export async function expectDownload(page: Page, options: ExpectDownloadOptions = {}): Promise<StatelockDownload> {
  const { predicate } = options;
  const timeout = options.timeout ?? defaultTimeout(page);
  const session = await statelockSession(page);
  if (!session) throw new Error("expectDownload: the page's browser is not connected through Statelock");
  return withCdp(page, async (cdp) => {
    const known = await knownDownloads(cdp);
    const deadline = timeout > 0 ? Date.now() + timeout : Infinity;
    for (;;) {
      // At least 1 ms: 0 would mean "no limit" to waitForDownload.
      const remaining = deadline === Infinity ? 0 : Math.max(1, deadline - Date.now());
      let info: DownloadInfo;
      try {
        info = await waitForDownload(cdp, known, remaining);
      } catch (error) {
        // A blocked download: name the session, as the Python SDK does (guard() looks it up).
        if (error instanceof StatelockPolicyViolationError && !error.sessionId) {
          throw new StatelockPolicyViolationError({ ...error.violation, session_id: session.session_id });
        }
        throw error;
      }
      const download = new StatelockDownload(page, info);
      if (!predicate || (await predicate(download))) return download;
      known.add(info.guid);
    }
  });
}

function downloadWaitOptions(optionsOrPredicate: unknown): ExpectDownloadOptions {
  if (typeof optionsOrPredicate === "function") return { predicate: optionsOrPredicate as DownloadPredicate };
  return (optionsOrPredicate ?? {}) as ExpectDownloadOptions;
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
    async function (this: { page(): Page; element(): ElementHandle }, files: UploadFile | UploadFile[], options?: unknown) {
      if (await statelockSession(this.page())) {
        await setInputFiles(this.page(), this.element(), files, uploadOptions(options));
        return;
      }
      return original.call(this, files, options);
    },
  );
}

function recordDefaultTimeout(original: AnyFunction): AnyFunction {
  return function (this: Page | BrowserContext, timeout: number) {
    defaultTimeouts.set(this, timeout);
    return original.call(this, timeout);
  };
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
  const contextProto = Object.getPrototypeOf(context);
  const locatorProto = Object.getPrototypeOf(page.locator("html"));
  const handleProto = Object.getPrototypeOf(handle);
  await handle.dispose();
  if (!existing) await context.close();
  else if (!hadPage) await page.close();

  patch(pageProto, "setInputFiles", (original) =>
    async function (this: Page, selector: string, files: UploadFile | UploadFile[], options?: unknown) {
      if (await statelockSession(this)) {
        await setInputFiles(this, selector, files, uploadOptions(options));
        return;
      }
      return original.call(this, selector, files, options);
    },
  );
  patch(locatorProto, "setInputFiles", (original) =>
    async function (this: Locator, files: UploadFile | UploadFile[], options?: unknown) {
      if (await statelockSession(this.page())) {
        await setInputFiles(this.page(), this, files, uploadOptions(options));
        return;
      }
      return original.call(this, files, options);
    },
  );
  patch(handleProto, "setInputFiles", (original) =>
    async function (this: ElementHandle, files: UploadFile | UploadFile[], options?: unknown) {
      const owner = await frameOwner(this);
      if (owner && (await statelockSession(owner))) {
        await setInputFiles(owner, this, files, uploadOptions(options));
        return;
      }
      return original.call(this, files, options);
    },
  );
  patch(pageProto, "waitForEvent", (original) =>
    async function (this: Page, event: string, optionsOrPredicate?: unknown, ...rest: unknown[]) {
      if (event === "download" && (await statelockSession(this))) {
        return expectDownload(this, downloadWaitOptions(optionsOrPredicate));
      }
      return original.call(this, event, optionsOrPredicate, ...rest);
    },
  );
  patch(pageProto, "setDefaultTimeout", recordDefaultTimeout);
  patch(contextProto, "setDefaultTimeout", recordDefaultTimeout);
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

export interface ConnectPlaywrightOptions extends SessionUrlOptions {
  /** Passed to chromium.connectOverCDP (timeout, slowMo, ...). */
  connectOptions?: ConnectOverCDPOptions;
}

/** A Statelock session plus the Playwright browser connected to it. */
export interface PlaywrightSession {
  session: SessionUrl;
  browser: Browser;
  /** The first page in the governed browser context. */
  readonly page: Page;
  /** Run fn; if Statelock ends the session, throw StatelockPolicyViolationError (rule, reason). */
  guard<T>(fn: () => Promise<T>): Promise<T>;
  /** setInputFiles on `options.page` (default: page), without install(). */
  setInputFiles(
    target: string | Locator | ElementHandle,
    files: UploadFile | UploadFile[],
    options?: SetInputFilesOptions & { page?: Page },
  ): Promise<UploadedFile[]>;
  /** expectDownload on `options.page` (default: page), without install(). */
  expectDownload(options?: ExpectDownloadOptions & { page?: Page }): Promise<StatelockDownload>;
  /** Close the Playwright browser connection. */
  close(): Promise<void>;
}

/**
 * Create a Statelock session URL and connect Playwright over CDP: the counterpart of the
 * Python SDK's connect_playwright. serverUrl defaults to STATELOCK_URL, apiKey to
 * STATELOCK_API_KEY (see createSessionUrl).
 */
export async function connectPlaywright(
  chromium: Pick<BrowserType, "connectOverCDP">,
  options: ConnectPlaywrightOptions = {},
): Promise<PlaywrightSession> {
  const { connectOptions, ...sessionOptions } = options;
  const session = await createSessionUrl(sessionOptions);
  const browser = await chromium.connectOverCDP(session.cdpUrl, connectOptions);
  const firstPage = () => browser.contexts()[0].pages()[0];
  return {
    session,
    browser,
    get page() {
      return firstPage();
    },
    guard: (fn) => session.guard(fn),
    setInputFiles: (target, files, { page, ...rest } = {}) => setInputFiles(page ?? firstPage(), target, files, rest),
    expectDownload: ({ page, ...rest } = {}) => expectDownload(page ?? firstPage(), rest),
    close: () => browser.close(),
  };
}
