// SPDX-License-Identifier: Apache-2.0
/**
 * Statelock client for JavaScript and TypeScript agents.
 *
 * - createSessionUrl(): a single-use browser URL for any CDP framework (Playwright,
 *   Puppeteer, Stagehand v3), optionally with a saved login.
 * - session.guard() / guard(): turn a session Statelock ended into StatelockPolicyViolationError.
 * - Governed uploads and downloads over any CDP session (uploadFiles, waitForDownload, ...).
 * - Playwright: import { connectPlaywright, install } from "@statelock/client/playwright".
 *
 * Credential injection needs no SDK: fill a password field with "{{secret:<name>}}".
 */

export { StatelockClientError, type ClientOptions } from "./http.js";
export {
  createSessionUrl,
  deleteSavedSession,
  listSavedSessions,
  type SessionUrl,
  type SessionUrlOptions,
} from "./sessions.js";
export { StatelockPolicyViolationError, guard, type GuardOptions, type Violation } from "./violations.js";
export {
  StatelockDownloadError,
  StatelockResponse,
  cdpSender,
  knownDownloads,
  readDownload,
  setFileInputFiles,
  statelockFetch,
  uploadFiles,
  waitForDownload,
  type CdpSender,
  type DownloadInfo,
  type FetchOptions,
  type StatelockSessionInfo,
  type UploadFile,
  type UploadedFile,
} from "./cdp.js";

/** A secret placeholder for credential injection: Statelock types the value. */
export function secret(name: string): string {
  return `{{secret:${name}}}`;
}
