// SPDX-License-Identifier: Apache-2.0
/**
 * Session URLs: open a Statelock session from one URL, without headers.
 *
 *   const session = await createSessionUrl();            // STATELOCK_URL, STATELOCK_API_KEY
 *   const browser = await chromium.connectOverCDP(session.cdpUrl);
 *   // Stagehand v3: new Stagehand({ env: "LOCAL", localBrowserLaunchOptions: { cdpUrl: session.wsUrl } })
 *
 * The URL works once and expires after a few minutes.
 */

import { type ClientOptions, StatelockClientError, apiKey, call, serverUrl } from "./http.js";
import { type StatelockPolicyViolationError, guard, lookupViolation } from "./violations.js";

export interface SessionUrlOptions extends ClientOptions {
  ttlSeconds?: number;
  /** Restore this saved browser session (cookies, localStorage) before the agent connects. */
  savedSession?: string;
  /** Save the browser state under savedSession when the session ends cleanly (no violation). */
  saveSession?: boolean;
}

export interface SessionUrl {
  sessionId: string;
  agentId: string;
  /** http(s)://.../sessions/<token>: for frameworks that take a browser URL (Playwright connectOverCDP). */
  cdpUrl: string;
  /** ws(s)://.../sessions/<token>/devtools: the WebSocket itself (Stagehand v3 cdpUrl). */
  wsUrl: string;
  expiresAt: string;
  savedSession: string | null;
  /** Run fn; if Statelock ends the session, throw StatelockPolicyViolationError (rule, reason). */
  guard<T>(fn: () => Promise<T>): Promise<T>;
  /**
   * The violation that ended this session, or null if there is none. A lookup Statelock
   * refuses (a wrong key) or cannot answer throws StatelockClientError: that is not "no
   * violation". apiKey defaults to the key the session was created with.
   */
  violation(apiKey?: string): Promise<StatelockPolicyViolationError | null>;
}

/** Ask Statelock for a single-use session URL for the agent the key belongs to. */
export async function createSessionUrl(options: SessionUrlOptions = {}): Promise<SessionUrl> {
  if (options.saveSession && !options.savedSession) {
    throw new StatelockClientError("saveSession needs savedSession");
  }
  const body: Record<string, unknown> = {};
  if (options.agentId) body.agent_id = options.agentId;
  if (options.ttlSeconds) body.ttl_seconds = options.ttlSeconds;
  if (options.savedSession) {
    body.saved_session_name = options.savedSession;
    body.save_session = Boolean(options.saveSession);
  }
  const payload = await call("POST", "/sessions", options, body);
  const server = serverUrl(options);
  const key = apiKey(options);
  const sessionId = String(payload.session_id);
  return {
    sessionId,
    agentId: String(payload.agent_id),
    cdpUrl: String(payload.cdp_url),
    wsUrl: String(payload.ws_url),
    expiresAt: String(payload.expires_at),
    savedSession: typeof payload.saved_session_name === "string" ? payload.saved_session_name : null,
    guard: <T>(fn: () => Promise<T>) => guard(fn, { serverUrl: server, sessionId, apiKey: key }),
    violation: (lookupKey?: string) => lookupViolation(server, sessionId, lookupKey ?? key),
  };
}

/** Names of the calling agent's saved browser sessions. */
export async function listSavedSessions(options: ClientOptions = {}): Promise<string[]> {
  const query = options.agentId ? `?agent_id=${encodeURIComponent(options.agentId)}` : "";
  const payload = await call("GET", `/saved-sessions${query}`, options);
  const names = payload.saved_sessions;
  return Array.isArray(names) ? names.map(String) : [];
}

/** Delete one of the calling agent's saved browser sessions. False if it did not exist. */
export async function deleteSavedSession(name: string, options: ClientOptions = {}): Promise<boolean> {
  const query = options.agentId ? `?agent_id=${encodeURIComponent(options.agentId)}` : "";
  const payload = await call("DELETE", `/saved-sessions/${encodeURIComponent(name)}${query}`, options);
  return payload.deleted === true;
}
