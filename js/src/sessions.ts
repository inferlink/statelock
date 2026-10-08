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

import { StatelockPolicyViolationError, guard } from "./violations.js";

export const API_KEY_ENV = "STATELOCK_API_KEY";
export const SERVER_ENV = "STATELOCK_URL";
const REQUEST_TIMEOUT_MS = 10_000;

/** Statelock refused or could not answer a request (session URLs, saved sessions,
 *  violation lookups). `status` is the HTTP status when Statelock answered. */
export class SessionUrlError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "SessionUrlError";
    this.status = status;
  }
}

export interface ClientOptions {
  /** The Statelock server, e.g. http://localhost:8010 (default: STATELOCK_URL). */
  serverUrl?: string;
  /** The agent's key (default: STATELOCK_API_KEY). */
  apiKey?: string;
  /** Only when the proxy's authentication is off. */
  agentId?: string;
}

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
}

function env(name: string): string | undefined {
  const value = typeof process !== "undefined" ? process.env[name] : undefined;
  return value ? value : undefined;
}

export function serverUrl(options: ClientOptions): string {
  const server = (options.serverUrl ?? env(SERVER_ENV) ?? "").replace(/\/+$/, "");
  if (!server) {
    throw new SessionUrlError("no Statelock server URL (pass serverUrl or set STATELOCK_URL)");
  }
  return server;
}

export function apiKey(options: ClientOptions): string | undefined {
  return options.apiKey ?? env(API_KEY_ENV);
}

export async function call(
  method: string,
  path: string,
  options: ClientOptions,
  body?: Record<string, unknown>,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<Record<string, unknown>> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const key = apiKey(options);
  if (key) headers.Authorization = `Bearer ${key}`;
  const server = serverUrl(options);
  let response: Response;
  let text: string;
  try {
    response = await fetch(`${server}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    text = await response.text();
  } catch (error) {
    // A refused connection, a DNS failure or the timeout: one error type for callers.
    throw new SessionUrlError(`could not reach Statelock at ${server}: ${String(error)}`, undefined, { cause: error });
  }
  if (!response.ok) {
    throw new SessionUrlError(`Statelock refused the request (${response.status}): ${text.slice(0, 300)}`, response.status);
  }
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (error) {
    throw new SessionUrlError(`Statelock at ${server} did not answer with JSON`, response.status, { cause: error });
  }
  return payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
}

/** Ask Statelock for a single-use session URL for the agent the key belongs to. */
export async function createSessionUrl(options: SessionUrlOptions = {}): Promise<SessionUrl> {
  if (options.saveSession && !options.savedSession) {
    throw new SessionUrlError("saveSession needs savedSession");
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

export { StatelockPolicyViolationError };
