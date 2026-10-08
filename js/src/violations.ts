// SPDX-License-Identifier: Apache-2.0
/** Violation errors and the lookup of a session's violation. */

import { StatelockClientError, call } from "./http.js";

export const VIOLATION_MARKER = "STATELOCK_POLICY_VIOLATION";
const LOOKUP_TIMEOUT_MS = 2_000;
const SHORT_TYPES: Record<string, string> = { pre: "pre_condition", post: "post_condition" };

export interface Violation {
  violation_type?: string | null;
  rule?: string | null;
  reason?: string | null;
  policy_id?: string | null;
  agent_id?: string | null;
  session_id?: string | null;
  sequence?: number | null;
  [key: string]: unknown;
}

/** Thrown when Statelock blocks an action or a post-condition fails. */
export class StatelockPolicyViolationError extends Error {
  readonly violation: Violation;
  readonly violationType: string | null;
  readonly rule: string | null;
  readonly reason: string;
  readonly policyId: string | null;
  readonly agentId: string | null;
  readonly sessionId: string | null;
  readonly sequence: number | null;

  constructor(violation: Violation, options?: { cause?: unknown }) {
    const reason = violation.reason || "Statelock policy violation";
    super(
      `[${violation.violation_type ?? null}/${violation.rule ?? null}] ${reason} ` +
        `(policy=${violation.policy_id ?? null} session=${violation.session_id ?? null} action=${violation.sequence ?? null})`,
      options,
    );
    this.name = "StatelockPolicyViolationError";
    this.violation = violation;
    this.violationType = violation.violation_type ?? null;
    this.rule = violation.rule ?? null;
    this.reason = reason;
    this.policyId = violation.policy_id ?? null;
    this.agentId = violation.agent_id ?? null;
    this.sessionId = violation.session_id ?? null;
    this.sequence = violation.sequence ?? null;
  }
}

/** A violation embedded in error text (the in-band CDP error or the WebSocket close reason). */
export function decodeViolation(message: string): Violation | null {
  const index = message.indexOf(VIOLATION_MARKER);
  if (index < 0) return null;
  const rest = message.slice(index + VIOLATION_MARKER.length).trimStart();
  if (!rest.startsWith("{")) return null;
  // The JSON object ends at its matching brace (strings may contain braces).
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < rest.length; i++) {
    const c = rest[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (c === "\\") escaped = true;
      else if (c === '"') inString = false;
      continue;
    }
    if (c === '"') inString = true;
    else if (c === "{") depth++;
    else if (c === "}" && --depth === 0) {
      try {
        const payload = JSON.parse(rest.slice(0, i + 1)) as Record<string, unknown>;
        if ("s" in payload || "t" in payload) {
          const short = String(payload.t ?? "");
          return {
            violation_type: SHORT_TYPES[short] ?? (short || null),
            rule: (payload.r as string) ?? null,
            session_id: (payload.s as string) ?? null,
            sequence: (payload.n as number) ?? null,
          };
        }
        return payload as Violation;
      } catch {
        return null;
      }
    }
  }
  return null;
}

/**
 * GET /violations/<session_id>: the recorded violation, or null when there is none (404).
 * Any other failure (a wrong key, a server error, no answer) throws StatelockClientError:
 * it does not mean the session had no violation. apiKey defaults to STATELOCK_API_KEY.
 */
export async function fetchViolation(
  serverUrl: string,
  sessionId: string,
  apiKey?: string,
): Promise<Violation | null> {
  const origin = new URL(serverUrl.replace(/^ws(s?):/, "http$1:")).origin;
  try {
    const path = `/violations/${encodeURIComponent(sessionId)}`;
    const payload = await call("GET", path, { serverUrl: origin, apiKey }, undefined, LOOKUP_TIMEOUT_MS);
    return Object.keys(payload).length ? (payload as Violation) : null;
  } catch (error) {
    if (error instanceof StatelockClientError && error.status === 404) return null;
    throw error;
  }
}

/** Error text that means the browser connection closed (Playwright, Puppeteer, Stagehand v3). */
const CLOSED_TEXTS = ["has been closed", "Target closed", "Transport closed", "socket-close", "WebSocket is not open", "CDP connection closed"];

/** Only a closed session or the marker can mean Statelock ended the session. */
function mayBeViolation(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const text = `${error.name}: ${error.message}`;
  return error.name === "TargetClosedError" || text.includes(VIOLATION_MARKER) || CLOSED_TEXTS.some((t) => text.includes(t));
}

/** Errors that carry the record Statelock keeps (GET /violations): guard() does not look them up again. */
const recorded = new WeakSet<StatelockPolicyViolationError>();

function recordedError(violation: Violation, cause?: unknown): StatelockPolicyViolationError {
  const error = new StatelockPolicyViolationError(violation, cause === undefined ? undefined : { cause });
  recorded.add(error);
  return error;
}

/** session.violation(): the recorded violation as an error, or null (see fetchViolation). */
export async function lookupViolation(
  serverUrl: string,
  sessionId: string,
  apiKey?: string,
): Promise<StatelockPolicyViolationError | null> {
  const found = await fetchViolation(serverUrl, sessionId, apiKey);
  return found ? recordedError(found) : null;
}

export interface GuardOptions {
  serverUrl?: string;
  sessionId?: string;
  apiKey?: string;
  /**
   * While fn runs, ask Statelock every watchIntervalMs whether it ended the session
   * (default 1000; 0 turns it off). Needed for frameworks whose pending calls never
   * settle when the browser connection closes (Stagehand v3).
   */
  watchIntervalMs?: number;
}

/**
 * Run fn and turn errors caused by a Statelock violation into StatelockPolicyViolationError.
 * Other errors (timeouts, missing elements) pass through unchanged, without a lookup.
 * A lookup Statelock refuses (a wrong key) throws StatelockClientError, except when the
 * violation is already known (a blocked download): then that violation is thrown.
 */
export async function guard<T>(fn: () => Promise<T>, options: GuardOptions = {}): Promise<T> {
  const { serverUrl, sessionId, apiKey } = options;
  const interval = options.watchIntervalMs ?? 1000;
  let stopWatching = () => {};
  const watched =
    serverUrl && sessionId && interval > 0
      ? new Promise<never>((_, reject) => {
          let stopped = false;
          let timer: ReturnType<typeof setTimeout> | undefined;
          // The next lookup starts only after the last one answered: no pile-up on a slow server.
          const tick = async () => {
            let violation: Violation | null;
            try {
              violation = await fetchViolation(serverUrl, sessionId, apiKey);
            } catch (error) {
              if (!stopped) reject(error); // a refused lookup (a wrong key) fails the run, loudly
              return;
            }
            if (stopped) return;
            if (violation) {
              reject(recordedError(violation));
              return;
            }
            schedule();
          };
          const schedule = () => {
            timer = setTimeout(() => void tick(), interval);
            timer.unref?.();
          };
          schedule();
          stopWatching = () => {
            stopped = true;
            clearTimeout(timer);
          };
        })
      : null;
  watched?.catch(() => undefined); // a late result after fn settled is not an unhandled rejection
  try {
    return await (watched ? Promise.race([fn(), watched]) : fn());
  } catch (error) {
    if (error instanceof StatelockPolicyViolationError) {
      // The watcher's error already is the record. One raised by an SDK helper (a blocked
      // download) is completed with it; if that lookup fails, the known violation stands.
      const lookupId = sessionId ?? error.sessionId ?? undefined;
      if (recorded.has(error) || !serverUrl || !lookupId) throw error;
      const details = await fetchViolation(serverUrl, lookupId, apiKey).catch(() => null);
      throw details ? recordedError({ ...error.violation, ...details }, error) : error;
    }
    if (!mayBeViolation(error)) throw error;
    let violation: Violation = decodeViolation(String((error as Error).message)) ?? {};
    const lookupId = sessionId ?? (violation.session_id || undefined);
    if (serverUrl && lookupId) {
      const details = await fetchViolation(serverUrl, lookupId, apiKey);
      if (details) throw recordedError({ ...violation, ...details }, error);
    }
    if (!Object.keys(violation).length) throw error;
    throw new StatelockPolicyViolationError(violation, { cause: error });
  } finally {
    stopWatching();
  }
}
