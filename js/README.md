# @statelock/client

Statelock client for JavaScript and TypeScript agents (Node 18+):

- session URLs, with saved logins;
- violation errors;
- governed uploads and downloads;
- governed HTTP requests (`statelockFetch`, policy `request_access`).

An agent needs no other Statelock code.

It is not yet on npm: build it from this folder (`npm ci` runs the build), and depend on it by path (`"@statelock/client": "file:../statelock/js"`), as `examples/ojs-ts` does.

Compared with the Python SDK, it does not patch Playwright's `page.request` (use `statelockFetch`), has no `form`/`params` request options or request error class (a declined request rejects with the CDP error), and has no LangChain wrapper. `page.waitForEvent("download")` counts downloads that start after the wait starts; start it together with the click, as the example does.

```ts
import { chromium } from "playwright-core";
import { createSessionUrl, secret } from "@statelock/client";
import { install } from "@statelock/client/playwright";

const session = await createSessionUrl({ savedSession: "portal-login", saveSession: true }); // STATELOCK_URL, STATELOCK_API_KEY
const browser = await chromium.connectOverCDP(session.cdpUrl);
await install(browser);                                    // Playwright's own setInputFiles / waitForEvent("download")
const page = browser.contexts()[0].pages()[0];

await session.guard(async () => {                          // a violation throws StatelockPolicyViolationError
  await page.goto("https://portal.example.com/login");
  await page.fill("#password", secret("portal_password")); // Statelock types the password
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#report")]);
  await download.saveAs("report.pdf");                     // checked by Statelock, SHA-256 verified
});
```

## API

| Function | What it does |
|---|---|
| `createSessionUrl({ serverUrl, apiKey, agentId, ttlSeconds, savedSession, saveSession })` | A single-use session URL. `cdpUrl` (http) is for Playwright `connectOverCDP` and Puppeteer `browserURL`; `wsUrl` is the WebSocket itself (Stagehand v3 `cdpUrl`). `serverUrl` defaults to `STATELOCK_URL`, `apiKey` to `STATELOCK_API_KEY`. |
| `session.guard(fn)` / `guard(fn, { serverUrl, sessionId, apiKey })` | Runs `fn`. If Statelock ends the session, it throws `StatelockPolicyViolationError` (`rule`, `reason`, `policyId`, `agentId`, `sessionId`, `sequence`). While `fn` runs, it also asks Statelock once a second whether the session ended (`watchIntervalMs`, 0 to turn off). This is needed for frameworks whose calls hang when the connection closes. A lookup Statelock refuses (a wrong key) or cannot answer throws `SessionUrlError` (`status`). |
| `listSavedSessions()`, `deleteSavedSession(name)` | The agent's saved logins. |
| `secret(name)` | The placeholder `{{secret:name}}` for credential injection. |
| `uploadFiles(cdp, files)`, `setFileInputFiles(cdp, node, files)` | Governed uploads over any CDP session. Each stored file is `{ path, name, size, sha256, mimeType }`. |
| `knownDownloads(cdp)`, `waitForDownload(cdp, known)`, `readDownload(cdp, info)` | Governed downloads over any CDP session. |
| `statelockFetch(cdp, url, { method, headers, data })` | Governed HTTP: Statelock makes the request in the page, with the browser's session (policy `request_access`). Header values may use `secret()`. |
| `cdpSender(x)` | A CDP session from a Playwright/Puppeteer `CDPSession` or a Stagehand v3 page (`sendCDP`). |
| `install(browser)` (from `@statelock/client/playwright`) | Playwright's `setInputFiles` (Page, Locator, ElementHandle), `FileChooser.setFiles` (from `page.waitForEvent("filechooser")` or a `page.on("filechooser")` listener) and `page.waitForEvent("download")` go through Statelock. Browsers not behind Statelock are untouched. |

## Frameworks

- **Playwright (`playwright-core` 1.40+).** `connectOverCDP(session.cdpUrl)` connects. Call `install(browser)` for the file APIs. `page.on("download")` listeners are not covered.
- **Stagehand v3 (3.7).** `new Stagehand({ env: "LOCAL", localBrowserLaunchOptions: { cdpUrl: session.wsUrl } })` connects. Its clicks are real CDP input, so they are governed like any click. It has no download API, so use `cdpSender(page)` with `waitForDownload` / `readDownload`. When the connection closes, Stagehand v3 leaves pending calls unsettled, and `session.guard` reports the violation anyway. See `examples/ojs-ts`.
- **Stagehand v4 is not supported.** Its driver runs as a Chrome extension inside the browser, not over CDP, so a CDP proxy cannot govern it.
- **Puppeteer.** `puppeteer.connect({ browserURL: session.cdpUrl })` connects. Use the CDP helpers with `page.createCDPSession()`.

## Development

```bash
npm ci              # also builds dist/ (the prepare script)
npm test            # builds, then runs test/ against the Python test proxy (tests/browser_support.py)
```

The tests start `test/server.py` with `python3`, or `PYTHON` if set. They need the Python package with its dev extras (`pip install -e "..[dev]"`) and Playwright's Chromium. `CHROMIUM_PATH` sets the Chromium for the plain-browser test. `STATELOCK_TEST_LOGS=1` shows the proxy's logs.
