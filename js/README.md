# @statelock/client

Statelock client for JavaScript and TypeScript agents (Node 20+):

- session URLs, with saved logins;
- violation errors;
- governed uploads and downloads;
- governed HTTP requests (`statelockFetch`, policy `request_access`).

An agent needs no other Statelock code.

It is not yet on npm: build it from this folder (`npm ci` runs the build), and depend on it by path (`"@statelock/client": "file:../statelock/js"`), as `examples/ojs-ts` does.

The names match the Python SDK's, in JavaScript case (`statelock_session` is `statelockSession`, `expect_download` is `expectDownload`, ...). Compared with the Python SDK, it does not patch Playwright's `page.request` (use `statelockFetch`), has no `form`/`params` request options or request error class (a declined request rejects with the CDP error), and has no LangChain wrapper. `page.waitForEvent("download")` and `expectDownload` count downloads that start after the wait starts; start the wait together with the click, as the example does.

```ts
import { chromium } from "playwright-core";
import { secret } from "@statelock/client";
import { connectPlaywright, install } from "@statelock/client/playwright";

// STATELOCK_URL, STATELOCK_API_KEY; the saved login is restored, and saved again at a clean end.
const governed = await connectPlaywright(chromium, { savedSession: "portal-login", saveSession: true });
await install(governed.browser);                           // Playwright's own setInputFiles / waitForEvent("download")
const page = governed.page;

await governed.guard(async () => {                         // a violation throws StatelockPolicyViolationError
  await page.goto("https://portal.example.com/login");
  await page.fill("#password", secret("portal_password")); // Statelock types the password
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#report")]);
  await download.saveAs("report.pdf");                     // checked by Statelock, SHA-256 verified
});
await governed.close();
```

`connectPlaywright` is `createSessionUrl` plus `chromium.connectOverCDP(session.cdpUrl)`, like the Python SDK's `connect_playwright`; use those two directly for any other CDP framework. Without `install()`, use the session's own `governed.setInputFiles(target, files)` and `governed.expectDownload()`.

## API

From `@statelock/client` (any CDP framework):

| Function | What it does |
|---|---|
| `createSessionUrl({ serverUrl, apiKey, agentId, ttlSeconds, savedSession, saveSession })` | A single-use session URL. `cdpUrl` (http) is for Playwright `connectOverCDP` and Puppeteer `browserURL`; `wsUrl` is the WebSocket itself (Stagehand v3 `cdpUrl`). `serverUrl` defaults to `STATELOCK_URL`, `apiKey` to `STATELOCK_API_KEY` (`""` sends no key). |
| `session.guard(fn)` / `guard(fn, { serverUrl, sessionId, apiKey })` | Runs `fn`. If Statelock ends the session, it throws `StatelockPolicyViolationError` (`rule`, `reason`, `policyId`, `agentId`, `sessionId`, `sequence`). While `fn` runs, it also asks Statelock once a second whether the session ended (`watchIntervalMs`, 0 to turn off). This is needed for frameworks whose calls hang when the connection closes. A lookup Statelock refuses (a wrong key) or cannot answer throws `StatelockClientError` (`status`), except when the violation is already known (a blocked download): that violation is thrown. |
| `session.violation(apiKey?)` | The violation that ended the session (`StatelockPolicyViolationError`), or `null`. A refused or failed lookup throws `StatelockClientError`: it is not "no violation". |
| `listSavedSessions()`, `deleteSavedSession(name)` | The agent's saved logins. |
| `secret(name)` | The placeholder `{{secret:name}}` for credential injection. |
| `uploadFiles(cdp, files)`, `setFileInputFiles(cdp, node, files)` | Governed uploads over any CDP session. A file is a path or `{ name, mimeType, buffer }`; `buffer` is a Buffer, TypedArray, DataView, ArrayBuffer or string (UTF-8), anything else is a `TypeError`. A path's MIME type comes from its extension (the same table as the Python SDK). Each stored file is `{ path, name, size, sha256, mimeType }`. |
| `knownDownloads(cdp)`, `waitForDownload(cdp, known, timeoutMs)`, `readDownload(cdp, info)` | Governed downloads over any CDP session. `timeoutMs` defaults to 30000; 0 waits without a limit. A blocked download throws `StatelockPolicyViolationError` with the rule Statelock reports (`null` if none); `guard` completes it with the recorded violation. |
| `statelockFetch(cdp, url, { method, headers, data })` | Governed HTTP: Statelock makes the request in the page, with the browser's session (policy `request_access`). Header values may use `secret()`. Returns a `StatelockResponse` with Playwright's `APIResponse` methods: `status()`, `statusText()`, `ok()`, `url()`, `headers()`, `headersArray()`, and async `body()`, `text()`, `json()`. |
| `cdpSender(x)` | A CDP session from a Playwright/Puppeteer `CDPSession` or a Stagehand v3 page (`sendCDP`). |
| `StatelockPolicyViolationError`, `StatelockClientError`, `StatelockDownloadError` | The error classes: a violation; a request Statelock refused or could not answer (`status`); a download that was canceled or did not complete in time. |

From `@statelock/client/playwright`:

| Function | What it does |
|---|---|
| `connectPlaywright(chromium, { serverUrl, apiKey, agentId, ttlSeconds, savedSession, saveSession, connectOptions })` | A session URL, connected over CDP. Returns `{ session, browser, page, guard(fn), setInputFiles(target, files, { page, timeout }), expectDownload({ page, predicate, timeout }), close() }`; `connectOptions` go to `connectOverCDP`. `page` defaults to the first page. |
| `install(browser)`, `uninstall()` | Playwright's `setInputFiles` (Page, Locator, ElementHandle), `FileChooser.setFiles` (from `page.waitForEvent("filechooser")` or a `page.on("filechooser")` listener) and `page.waitForEvent("download")` go through Statelock; `uninstall()` restores them. Options Statelock cannot honour (`strict: false`, unknown ones) are refused, not dropped. Browsers not behind Statelock are untouched. |
| `setInputFiles(page, target, files, { timeout })` | A governed upload into a file input of the page's main frame (`target`: a selector, Locator or ElementHandle). |
| `expectDownload(page, { predicate, timeout })` | The next download the page starts that `predicate` accepts, after Statelock checked it: a `StatelockDownload` (`saveAs`, `path`, `readBytes`, `suggestedFilename`, `url`, `sha256`). `timeout` defaults to the page's or context's `setDefaultTimeout` (set after `install()`), else 30000; 0 waits without a limit. |
| `statelockSession(page)` | `{ session_id, agent_id }` if the page's browser is behind Statelock, else `null`. |

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
