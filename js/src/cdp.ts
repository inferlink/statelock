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

import { StatelockPolicyViolationError } from "./violations.js";

/** Per CDP message; stays well under the proxy's 16 MiB WebSocket message limit. */
export const CHUNK_BYTES = 4 * 1024 * 1024;
export const SESSION_COMMAND = "Statelock.session";
const DOWNLOAD_POLL_MS = 100;
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

/** A file to upload: a path, or Playwright's FilePayload shape. */
export type UploadFile = string | { name: string; mimeType?: string; buffer: Uint8Array | string };

export interface UploadedFile {
  path: string; // on the proxy; only DOM.setFileInputFiles on the same session may use it
  name: string;
  size: number;
  sha256: string;
  mimeType: string | null;
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
export async function statelockSession(cdp: CdpSender): Promise<{ session_id: string; agent_id: string } | null> {
  let result: Record<string, unknown>;
  try {
    result = await send(cdp, SESSION_COMMAND);
  } catch (error) {
    if (String(error instanceof Error ? error.message : error).includes(UNKNOWN_COMMAND_TEXT)) return null;
    throw error;
  }
  if (typeof result.session_id !== "string") throw new Error(`unexpected ${SESSION_COMMAND} answer`);
  return result as { session_id: string; agent_id: string };
}

const MIME_TYPES: Record<string, string> = {
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".csv": "text/csv",
  ".json": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

async function payload(item: UploadFile): Promise<{ name: string; mimeType: string | null; data: Buffer }> {
  if (typeof item === "string") {
    const name = basename(item);
    const extension = name.includes(".") ? name.slice(name.lastIndexOf(".")).toLowerCase() : "";
    return { name, mimeType: MIME_TYPES[extension] ?? null, data: await readFile(item) };
  }
  const data = typeof item.buffer === "string" ? Buffer.from(item.buffer, "utf8") : Buffer.from(item.buffer);
  return { name: item.name, mimeType: item.mimeType ?? null, data };
}

/** Stream files to Statelock over the session. Returns the proxy-side files (name, size, SHA-256). */
export async function uploadFiles(cdp: CdpSender, files: UploadFile | UploadFile[]): Promise<UploadedFile[]> {
  const uploaded: UploadedFile[] = [];
  for (const item of Array.isArray(files) ? files : [files]) {
    const { name, mimeType, data } = await payload(item);
    const begun = await send(cdp, "Statelock.uploadFileBegin", { name, mimeType });
    const uploadId = begun.uploadId;
    for (let offset = 0; offset < data.length; offset += CHUNK_BYTES) {
      const chunk = data.subarray(offset, offset + CHUNK_BYTES).toString("base64");
      await send(cdp, "Statelock.uploadFileChunk", { uploadId, data: chunk });
    }
    uploaded.push((await send(cdp, "Statelock.uploadFileEnd", { uploadId })) as unknown as UploadedFile);
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

export async function listDownloads(cdp: CdpSender): Promise<Record<string, unknown>[]> {
  const result = await send(cdp, "Statelock.downloads");
  const downloads = result.downloads;
  return Array.isArray(downloads) ? downloads.filter((d) => d && typeof d === "object") : [];
}

/** The guids Statelock already knows: pass them to waitForDownload to wait for the next one. */
export async function knownDownloads(cdp: CdpSender): Promise<Set<string>> {
  return new Set((await listDownloads(cdp)).map((d) => String(d.guid)));
}

/**
 * Wait until a download not in `known` completes and passes Statelock's checks.
 * A download Statelock blocked throws StatelockPolicyViolationError (the session ends).
 */
export async function waitForDownload(cdp: CdpSender, known: Set<string>, timeoutMs = 30_000): Promise<DownloadInfo> {
  const deadline = Date.now() + timeoutMs;
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
        // guard() completes it with the violation Statelock recorded.
        throw new StatelockPolicyViolationError({
          rule: "download",
          reason: typeof item.reason === "string" ? item.reason : `Statelock blocked ${String(item.name)}`,
        });
      }
      if (item.state === "canceled") throw new StatelockDownloadError(`download ${String(item.name)} was canceled`);
    }
    await new Promise((resolve) => setTimeout(resolve, DOWNLOAD_POLL_MS));
  }
  throw new StatelockDownloadError(`no download completed within ${timeoutMs} ms`);
}

/** Read a checked download's bytes from the proxy, verifying its SHA-256. */
export async function readDownload(cdp: CdpSender, download: DownloadInfo): Promise<Buffer> {
  const chunks: Buffer[] = [];
  const digest = createHash("sha256");
  let offset = 0;
  for (;;) {
    const result = await send(cdp, "Statelock.downloadRead", { guid: download.guid, offset, length: CHUNK_BYTES });
    const chunk = Buffer.from(String(result.data ?? ""), "base64");
    digest.update(chunk);
    chunks.push(chunk);
    offset += chunk.length;
    if (result.eof || chunk.length === 0) break;
  }
  if (digest.digest("hex") !== download.sha256) {
    throw new StatelockDownloadError(`${download.name}: content does not match the recorded SHA-256`);
  }
  return Buffer.concat(chunks);
}

export interface FetchOptions {
  method?: string;
  headers?: Record<string, string>;
  /** Bytes or text; an object is sent as JSON. */
  data?: Uint8Array | string | Record<string, unknown> | unknown[];
}

export interface StatelockResponse {
  status: number;
  statusText: string;
  url: string;
  ok: boolean;
  headers: Record<string, string>;
  body: Buffer;
  text(): string;
  json(): unknown;
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
  const result = await send(cdp, "Statelock.fetch", {
    url,
    method: (options.method ?? "GET").toUpperCase(),
    headers,
    body: body ? body.toString("base64") : null,
  });
  const responseBody = Buffer.from(String(result.body ?? ""), "base64");
  const pairs = Array.isArray(result.headers) ? (result.headers as [string, string][]) : [];
  const status = Number(result.status ?? 0);
  return {
    status,
    statusText: String(result.statusText ?? ""),
    url: String(result.url ?? ""),
    ok: status >= 200 && status <= 299,
    headers: Object.fromEntries(pairs.map(([k, v]) => [k.toLowerCase(), v])),
    body: responseBody,
    text: () => responseBody.toString("utf8"),
    json: () => JSON.parse(responseBody.toString("utf8")),
  };
}
