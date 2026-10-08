// SPDX-License-Identifier: Apache-2.0
/** The client's one transport: JSON requests to the Statelock server. */

export const API_KEY_ENV = "STATELOCK_API_KEY";
export const SERVER_ENV = "STATELOCK_URL";
const REQUEST_TIMEOUT_MS = 10_000;

/** Statelock refused or could not answer a request (session URLs, saved sessions,
 *  violation lookups, guards). `status` is the HTTP status when Statelock answered. */
export class StatelockClientError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "StatelockClientError";
    this.status = status;
  }
}

export interface ClientOptions {
  /** The Statelock server, e.g. http://localhost:8010 (default: STATELOCK_URL). */
  serverUrl?: string;
  /** The agent's key (default: STATELOCK_API_KEY; "" sends no key). */
  apiKey?: string;
  /** Only when the proxy's authentication is off. */
  agentId?: string;
}

function env(name: string): string | undefined {
  const value = typeof process !== "undefined" ? process.env[name] : undefined;
  return value ? value : undefined;
}

export function serverUrl(options: ClientOptions): string {
  const server = (options.serverUrl ?? env(SERVER_ENV) ?? "").replace(/\/+$/, "");
  if (!server) {
    throw new StatelockClientError("no Statelock server URL (pass serverUrl or set STATELOCK_URL)");
  }
  return server;
}

/**
 * The key to send: options.apiKey, else STATELOCK_API_KEY. One rule in both SDKs:
 * undefined means "not given" (fall back), "" means "send no key" and is kept.
 */
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
    throw new StatelockClientError(`could not reach Statelock at ${server}: ${String(error)}`, undefined, { cause: error });
  }
  if (!response.ok) {
    throw new StatelockClientError(`Statelock refused the request (${response.status}): ${text.slice(0, 300)}`, response.status);
  }
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (error) {
    throw new StatelockClientError(`Statelock at ${server} did not answer with JSON`, response.status, { cause: error });
  }
  return payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
}
