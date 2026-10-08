// SPDX-License-Identifier: Apache-2.0
/**
 * Statelock client for JavaScript and TypeScript agents.
 *
 * - createSessionUrl(): a single-use browser URL for any CDP framework (Playwright,
 *   Puppeteer, Stagehand v3), optionally with a saved login.
 * - guard(): turn a session Statelock ended into StatelockPolicyViolationError.
 * - Governed uploads and downloads over any CDP session (uploadFiles, waitForDownload, ...).
 * - Playwright's own file APIs: import { install } from "@statelock/client/playwright".
 *
 * Credential injection needs no SDK: fill a password field with "{{secret:<name>}}".
 */

export {
  API_KEY_ENV,
  SERVER_ENV,
  SessionUrlError,
  createSessionUrl,
  deleteSavedSession,
  listSavedSessions,
  type ClientOptions,
  type SessionUrl,
  type SessionUrlOptions,
} from "./sessions.js";
export {
  StatelockPolicyViolationError,
  VIOLATION_MARKER,
  decodeViolation,
  fetchViolation,
  guard,
  type GuardOptions,
  type Violation,
} from "./violations.js";
export {
  CHUNK_BYTES,
  StatelockDownloadError,
  cdpSender,
  knownDownloads,
  listDownloads,
  readDownload,
  setFileInputFiles,
  statelockFetch,
  statelockSession,
  uploadFiles,
  waitForDownload,
  type CdpSender,
  type DownloadInfo,
  type FetchOptions,
  type StatelockResponse,
  type UploadFile,
  type UploadedFile,
} from "./cdp.js";

/** A secret placeholder for credential injection: Statelock types the value. */
export function secret(name: string): string {
  return `{{secret:${name}}}`;
}
