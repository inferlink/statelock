// Starts the Python test proxy once per test file (see server.py).
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const PYTHON = process.env.PYTHON ?? "python3";

export async function startServer() {
  const script = fileURLToPath(new URL("./server.py", import.meta.url));
  // The proxy logs to stderr; shown with STATELOCK_TEST_LOGS=1.
  const stderr = process.env.STATELOCK_TEST_LOGS ? "inherit" : "ignore";
  const child = spawn(PYTHON, [script], { stdio: ["pipe", "pipe", stderr] });
  const lines = createInterface({ input: child.stdout });
  // The server prints one JSON line when it is up; if it dies first, fail instead of hanging.
  const started = once(lines, "line").then(([line]) => line);
  const exited = once(child, "exit").then(([code]) => {
    throw new Error(`test server exited with code ${code} before starting (STATELOCK_TEST_LOGS=1 shows why)`);
  });
  const timeout = new Promise((_, reject) =>
    setTimeout(() => reject(new Error("test server did not start within 60 s")), 60_000).unref(),
  );
  const info = JSON.parse(await Promise.race([started, exited, timeout]));
  exited.catch(() => {}); // the normal exit at stop() is not an error
  return { ...info, stop: async () => { child.stdin.end(); await once(child, "exit"); } };
}

export const key = (agent) => `slk_test_${agent}`;

/**
 * A governed session for `agent`: a session URL, connectOverCDP, its first page.
 * Pass `install` to patch Playwright's file APIs. close() never throws (the session may
 * already have ended): call it in finally.
 */
export async function openSession(server, agent = "flow_agent", { install } = {}) {
  const { chromium } = await import("playwright-core");
  const { createSessionUrl } = await import("../dist/index.js");
  const session = await createSessionUrl({ serverUrl: server.base, apiKey: key(agent) });
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  if (install) await install(browser);
  return { session, browser, page: browser.contexts()[0].pages()[0], close: () => browser.close().catch(() => undefined) };
}
