// SPDX-License-Identifier: Apache-2.0
/**
 * Governed uploads and downloads over any CDP session to a Statelock browser.
 *
 * Works with anything that can send a CDP command: Playwright's CDPSession,
 * Puppeteer's CDPSession, Stagehand v3's page sessions. The Statelock.* commands
 * are answered by the proxy (never by Chromium).
 */

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { basename } from "node:path";

import { mimeType } from "./mime.js";
import { StatelockPolicyViolationError } from "./violations.js";

/** Per CDP message; stays well under the proxy's 16 MiB WebSocket message limit. */
const CHUNK_BYTES = 4 * 1024 * 1024;
// Statelock's own CDP commands (the same names as statelock.wire in the Python package).
const SESSION_COMMAND = "Statelock.session";
const UPLOAD_BEGIN_COMMAND = "Statelock.uploadFileBegin";
const UPLOAD_CHUNK_COMMAND = "Statelock.uploadFileChunk";
const UPLOAD_END_COMMAND = "Statelock.uploadFileEnd";
const DOWNLOADS_COMMAND = "Statelock.downloads";
const DOWNLOAD_READ_COMMAND = "Statelock.downloadRead";
const REQUEST_COMMAND = "Statelock.fetch";
const DOWNLOAD_POLL_MS = 100;
export const DEFAULT_DOWNLOAD_TIMEOUT_MS = 30_000;
/** Chromium's answer to a method it does not have ("'Statelock.session' wasn't found"). */
const UNKNOWN_COMMAND_TEXT = "wasn't found";

export interface CdpSender {
  send(method: string, params?: Record<string, unknown>): Promise<unknown>;
}

/** A CdpSender from a framework object: anything with send() (Playwright, Puppeteer
 *  CDPSession) or sendCDP() (a Stagehand v3 page). */
export function cdpSender(target: CdpSender | { sendCDP(method: string, params?: object): Promise<unknown> }): CdpSender {
  if ("sendCDP" in target && typeof target.sendCDP === "function") {
    return { send: (method, params) => target.sendCDP(method, params ?? {}) };
  }
  return target as CdpSender;
}

/** A file to upload: a path, or Playwright's FilePayload shape (buffer: bytes, or text as UTF-8). */
export type UploadFile = string | { name: string; mimeType?: string; buffer: ArrayBufferView | ArrayBuffer | string };

export interface UploadedFile {
  path: string; // on the proxy; only DOM.setFileInputFiles on the same session may use it
  name: string;
  size: number;
  sha256: string;
  mimeType: string | null;
}

/** What Statelock.session answers for a browser behind Statelock. */
export interface StatelockSessionInfo {
  session_id: string;
  agent_id: string;
}

async function send(cdp: CdpSender, method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const result = await cdp.send(method, params);
  return result && typeof result === "object" ? (result as Record<string, unknown>) : {};
}

/**
 * {session_id, agent_id} when the CDP session is behind Statelock, null for a plain
 * browser (it does not know the command). Any other error (a closed page, a lost
 * connection) says nothing about the browser and is thrown.
 */
export async function sessionInfo(cdp: CdpSender): Promise<StatelockSessionInfo | null> {
  let result: Record<string, unknown>;
  try {
    result = await send(cdp, SESSION_COMMAND);
  } catch (error) {
    if (String(error instanceof Error ? error.message : error).includes(UNKNOWN_COMMAND_TEXT)) return null;
    throw error;
  }
  if (typeof result.session_id !== "string") throw new Error(`unexpected ${SESSION_COMMAND} answer`);
  return result as unknown as StatelockSessionInfo;
}

async function payload(item: UploadFile): Promise<{ name: string; mimeType: string | null; data: Buffer }> {
  if (typeof item === "string") {
    const name = basename(item);
    return { name, mimeType: mimeType(name), data: await readFile(item) };
  }
  return { name: item.name, mimeType: item.mimeType ?? null, data: bufferBytes(item.buffer) };
}

/**
 * A FilePayload's buffer as bytes. Views are taken byte for byte (Buffer.from on a
 * Uint16Array would truncate each element); anything else, or no buffer, is refused
 * rather than uploaded as its text or as an empty file.
 */
function bufferBytes(buffer: unknown): Buffer {
  if (typeof buffer === "string") return Buffer.from(buffer, "utf8");
  if (ArrayBuffer.isView(buffer)) return Buffer.from(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  if (buffer instanceof ArrayBuffer) return Buffer.from(buffer);
  const kind = buffer === null ? "null" : typeof buffer === "object" ? (buffer.constructor?.name ?? "object") : typeof buffer;
  throw new TypeError(`setInputFiles: buffer must be a Buffer, TypedArray, DataView, ArrayBuffer or string, not ${kind}`);
}

/** Stream files to Statelock over the session. Returns the proxy-side files (name, size, SHA-256). */
export async function uploadFiles(cdp: CdpSender, files: UploadFile | UploadFile[]): Promise<UploadedFile[]> {
  const uploaded: UploadedFile[] = [];
  for (const item of Array.isArray(files) ? files : [files]) {
    const { name, mimeType: type, data } = await payload(item);
    const begun = await send(cdp, UPLOAD_BEGIN_COMMAND, { name, mimeType: type });
    const uploadId = begun.uploadId;
    for (let offset = 0; offset < data.length; offset += CHUNK_BYTES) {
      const chunk = data.subarray(offset, offset + CHUNK_BYTES).toString("base64");
      await send(cdp, UPLOAD_CHUNK_COMMAND, { uploadId, data: chunk });
    }
    uploaded.push((await send(cdp, UPLOAD_END_COMMAND, { uploadId })) as unknown as UploadedFile);
  }
  return uploaded;
}

/** Upload files and set them on the file input with this DOM node (nodeId or backendNodeId of the same session). */
export async function setFileInputFiles(
  cdp: CdpSender,
  node: { nodeId?: number; backendNodeId?: number },
  files: UploadFile | UploadFile[],
): Promise<UploadedFile[]> {
  const uploaded = await uploadFiles(cdp, files);
  await send(cdp, "DOM.setFileInputFiles", { ...node, files: uploaded.map((file) => file.path) });
  return uploaded;
}

export class StatelockDownloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StatelockDownloadError";
  }
}

export interface DownloadInfo {
  guid: string;
  name: string;
  url: string;
  size: number;
  sha256: string;
}

async function listDownloads(cdp: CdpSender): Promise<Record<string, unknown>[]> {
  const result = await send(cdp, DOWNLOADS_COMMAND);
  const downloads = result.downloads;
  return Array.isArray(downloads) ? downloads.filter((d) => d && typeof d === "object") : [];
}

/** The guids Statelock already knows: pass them to waitForDownload to wait for the next one. */
export async function knownDownloads(cdp: CdpSender): Promise<Set<string>> {
  return new Set((await listDownloads(cdp)).map((d) => String(d.guid)));
}

/**
 * Wait until a download not in `known` completes and passes Statelock's checks.
 * `timeoutMs` 0 waits without a limit, as in Playwright. A download Statelock blocked
 * throws StatelockPolicyViolationError with the rule it reports (the session ends;
 * guard() completes it with the recorded violation).
 */
export async function waitForDownload(
  cdp: CdpSender,
  known: Set<string>,
  timeoutMs = DEFAULT_DOWNLOAD_TIMEOUT_MS,
): Promise<DownloadInfo> {
  const deadline = timeoutMs > 0 ? Date.now() + timeoutMs : Infinity;
  while (Date.now() < deadline) {
    for (const item of await listDownloads(cdp)) {
      if (known.has(String(item.guid))) continue;
      if (item.state === "completed") {
        return {
          guid: String(item.guid),
          name: String(item.name),
          url: String(item.url),
          size: Number(item.size ?? 0),
          sha256: String(item.sha256),
        };
      }
      if (item.state === "blocked") {
        throw new StatelockPolicyViolationError({
          rule: typeof item.rule === "string" ? item.rule : null,
          reason: typeof item.reason === "string" ? item.reason : `Statelock blocked ${String(item.name)}`,
        });
      }
      if (item.state === "canceled") throw new StatelockDownloadError(`download ${String(item.name)} was canceled`);
    }
    await new Promise((resolve) => setTimeout(resolve, DOWNLOAD_POLL_MS));
  }
  throw new StatelockDownloadError(`no download completed within ${timeoutMs} ms`);
}

/** Stream a checked download from the proxy in chunks, verifying its SHA-256 at the end. */
export async function streamDownload(
  cdp: CdpSender,
  download: DownloadInfo,
  write: (chunk: Buffer) => Promise<void>,
): Promise<void> {
  const digest = createHash("sha256");
  let offset = 0;
  for (;;) {
    const result = await send(cdp, DOWNLOAD_READ_COMMAND, { guid: download.guid, offset, length: CHUNK_BYTES });
    const chunk = Buffer.from(String(result.data ?? ""), "base64");
    digest.update(chunk);
    await write(chunk);
    offset += chunk.length;
    if (result.eof || chunk.length === 0) break;
  }
  if (digest.digest("hex") !== download.sha256) {
    throw new StatelockDownloadError(`${download.name}: content does not match the recorded SHA-256`);
  }
}

/** Read a checked download's bytes from the proxy, verifying its SHA-256. */
export async function readDownload(cdp: CdpSender, download: DownloadInfo): Promise<Buffer> {
  const chunks: Buffer[] = [];
  await streamDownload(cdp, download, async (chunk) => {
    chunks.push(chunk);
  });
  return Buffer.concat(chunks);
}

export interface FetchOptions {
  method?: string;
  headers?: Record<string, string>;
  /** Bytes or text; an object is sent as JSON. */
  data?: Uint8Array | string | Record<string, unknown> | unknown[];
}

/** The response of a governed request, with Playwright's APIResponse API. */
export class StatelockResponse {
  readonly #status: number;
  readonly #statusText: string;
  readonly #url: string;
  readonly #headers: [string, string][];
  readonly #body: Buffer;

  constructor(result: Record<string, unknown>) {
    this.#status = Number(result.status ?? 0);
    this.#statusText = String(result.statusText ?? "");
    this.#url = String(result.url ?? "");
    const pairs = Array.isArray(result.headers) ? (result.headers as unknown[]) : [];
    this.#headers = pairs.filter(Array.isArray).map(([name, value]) => [String(name), String(value)]);
    this.#body = Buffer.from(String(result.body ?? ""), "base64");
  }

  status(): number {
    return this.#status;
  }

  statusText(): string {
    return this.#statusText;
  }

  url(): string {
    return this.#url;
  }

  ok(): boolean {
    return this.#status >= 200 && this.#status <= 299;
  }

  /** Header names in lower case (the last value of a repeated header, as in the Python SDK). */
  headers(): Record<string, string> {
    return Object.fromEntries(this.#headers.map(([name, value]) => [name.toLowerCase(), value]));
  }

  headersArray(): { name: string; value: string }[] {
    return this.#headers.map(([name, value]) => ({ name, value }));
  }

  async body(): Promise<Buffer> {
    return this.#body;
  }

  async text(): Promise<string> {
    return this.#body.toString("utf8");
  }

  async json(): Promise<unknown> {
    return JSON.parse(this.#body.toString("utf8"));
  }

  async dispose(): Promise<void> {}
}

/**
 * Have Statelock make an HTTP request in the page, with the browser's session
 * (policy request_access). Header values may use {{secret:name}} placeholders.
 * A declined request rejects with the reason.
 */
export async function statelockFetch(cdp: CdpSender, url: string, options: FetchOptions = {}): Promise<StatelockResponse> {
  const headers = { ...(options.headers ?? {}) };
  let body: Buffer | null = null;
  const { data } = options;
  if (typeof data === "string") body = Buffer.from(data, "utf8");
  else if (data instanceof Uint8Array) body = Buffer.from(data);
  else if (data !== undefined) {
    body = Buffer.from(JSON.stringify(data), "utf8");
    if (!Object.keys(headers).some((h) => h.toLowerCase() === "content-type")) headers["Content-Type"] = "application/json";
  }
  const result = await send(cdp, REQUEST_COMMAND, {
    url,
    method: (options.method ?? "GET").toUpperCase(),
    headers,
    body: body ? body.toString("base64") : null,
  });
  return new StatelockResponse(result);
}
