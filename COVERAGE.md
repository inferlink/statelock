# Coverage

This file lists the agent actions Statelock can currently analyze and those it cannot. Update it with every change to interception, attribution or the guard.

"Governed page" means a page whose URL matches a `target_url_contains` in one of the connecting agent's policies (every page if a policy has none). A path-only scope is a substring of `scheme://host[:port]/path` and can match on any origin; use an absolute URL scope to require an exact origin and a path starting with the scope's path (`/login` covers `/login/2fa`, not `/forum/login`). Query strings, fragments and URL user info do not count toward the match. The page guard is disabled for a policy with `allow_synthetic_events: true`.

Verified against Chromium 141.0.7390.37 (Playwright 1.56.0).

## Analyzed

### Real input (CDP `Input.*`)

| Action | What Statelock does |
|---|---|
| Mouse move, press, release, wheel (`Input.dispatchMouseEvent`) | State capture and pre-conditions on every event. `prohibit_click_text` checks the interactive element under the pointer at the press, found by a browser hit test that enters open and closed shadow roots and iframes in the page's process (fails closed if it can't be resolved or has no readable label; a click into an iframe Statelock cannot read, such as a cross-site one in its own process, is unresolved); moves, wheel and release are not activations. Post-conditions after release. Labels are normalized for whitespace, non-breaking spaces, formatting characters and Unicode presentation differences. |
| Key events (`Input.dispatchKeyEvent`) | State capture and pre-conditions on every event. `prohibit_click_text` applies to Enter and Space on the focused element, followed through shadow roots (open and closed) and iframes; focus in an iframe Statelock cannot read is unresolved and blocked. Enter and Space are recognised by `key`, `code`, `windowsVirtualKeyCode`, `text` or `unmodifiedText`, in every event type. Post-conditions run after Enter key-up, or immediately for Enter typed by itself (a `char`, or a `keyDown` named only by its text); `rawKeyDown` + `char` + `keyUp` is one key press. Space activation does not run post-conditions. |
| Text insertion (`Input.insertText`) | State capture and pre-conditions. |
| Touch (`Input.dispatchTouchEvent`) | State capture and pre-conditions on every event. `touchStart` is a press: `prohibit_click_text` and `trigger.click_text` check the element at the first touch point (fails closed if it can't be resolved). `touchEnd` is the release: post-conditions and a review approval apply to it, checked at its `touchStart` point. |
| Drag and drop (`Input.dispatchDragEvent`, Playwright `drag_and_drop`) | State capture and pre-conditions on every drag event. `prohibit_click_text` checks the element under the pointer at the `drop`. The `drop` replaces the mouse release, so post-conditions and `trigger.click_text` run after it. |
| IME composition (`Input.imeSetComposition`) | State capture (focused element) and pre-conditions. |
| Tap gesture (`Input.synthesizeTapGesture`) | Like a click: pre-conditions, `prohibit_click_text`, post-conditions. |
| Scroll and pinch gestures (`Input.synthesizeScrollGesture`, `Input.synthesizePinchGesture`) | State capture and pre-conditions. |
| Emulated touch (`Input.emulateTouchFromMouseEvent`) | Like a mouse event, including post-conditions after release. |
| File upload (`set_input_files` after `install()`, `governed.set_input_files`, or `Statelock.uploadFile*` then `DOM.setFileInputFiles`) | The agent streams each file to Statelock over its CDP connection; Statelock stores it for the session and records name, size and SHA-256. `DOM.setFileInputFiles` is then a governed action (state capture, pre-conditions, record with the file evidence). The browser fires trusted events, so the site sees a normal selection. Uploaded files are deleted when the session ends. |
| Download started by the page (link, form, script) | Checked when it begins (state capture, the page's policies, `restrict_downloads`) and when it completes (size limits, SHA-256); both recorded (`Statelock.download`). Released to the agent only after both checks (`expect_download`); a blocked download is cancelled or deleted and ends the session. The download listing gives the `rule` that blocked a download (`download_limit` for a size limit). |
| The agent's `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` | Answered by Statelock, not forwarded: downloads always go to Statelock's folder. `deny` is honoured. |
| `DOM.setFileInputFiles` naming any file not uploaded in the session | Refused (`file_upload_path`), so an agent cannot attach files from the Statelock host. |

### Session and protocol

| Action | What Statelock does |
|---|---|
| Unregistered agent, or a missing agent ID when authentication is off | Connection refused (`4401`). With API-key authentication, the agent ID may be omitted; the key identifies the agent, and a supplied ID must match it. |
| Invalid or reused session ID | Connection refused (`4409`). |
| Action in any tab or popup | State captured from the tab the command targets. A command on an unknown tab fails closed. |
| A message that is not JSON | Refused, never forwarded. Statelock forwards exactly the JSON it evaluated. |
| `Target.sendMessageToTarget` | Refused (`wrapped_command`). |
| `Debugger.setScriptSource` | Refused (`script_modification`). |
| `Page.navigate` (any frame) or `Target.createTarget` to `javascript:` | Refused (`javascript_url`). |
| `Page.navigate` (any frame) or `Target.createTarget` to anything but an http(s) URL or exactly `about:blank` (`file:`, `data:`, `blob:`, `chrome:`, `view-source:`, an empty URL); `Network.loadNetworkResource` to anything but http(s) | Refused (`navigation_url`). The browser runs on the Statelock host. |
| `Target.exposeDevToolsProtocol`, `Network.replayXHR`, `Page.setDocumentContent` | Refused (`ungoverned_command`): page code with its own CDP channel, a site request re-sent with the user's cookies, a document written by the agent. |
| Commands that name Statelock's isolated worlds (`worldName`, `executionContextName`), `Runtime.addBinding` with the guard's binding, or commands that target a Statelock execution context (`contextId`, `executionContextId`, `uniqueContextId`) | Refused (`statelock_internals`). |
| Agent network interception: any `Fetch.*` command, `Network.setRequestInterception`, `Network.setBlockedURLs` (Playwright `page.route`, `route.fulfill`, context HTTP credentials) | Refused (`agent_network_interception`). |
| Agent-selected browser proxy (`Target.createBrowserContext` with `proxyServer` or `proxyBypassList`), `Security.setIgnoreCertificateErrors` with `ignore: true`, `Security.setOverrideCertificateErrors` with `override: true`, or `Security.handleCertificateError` | Refused (`agent_network_interception`); the agent cannot substitute or decrypt browser traffic, or accept invalid certificates, through those commands. |
| Code sent in `Page.reload.scriptToEvaluateOnLoad` | Tagged as agent code, like `Runtime.evaluate` and `Debugger.evaluateOnCallFrame`, so requests it starts are subject to agent-code request checks. |
| Failed state capture | Action blocked (`state_capture`). Page state (URL, text, fields, the target element) is read in Statelock's own isolated world; a failure of that script blocks the action too. |
| The browser process | Started with a minimal environment, not the proxy's, in its own process group, with CDP over `--remote-debugging-pipe`: Chromium opens no DevTools TCP port, so pages cannot reach `/json/*` or the browser endpoint. Statelock's and the agent's CDP traffic share the pipe but stay separate: each sees only the sessions it attached, and a command on the other's session is answered "not found". Stopping sends SIGTERM to the group, then SIGKILL after 5 s; on Linux Chromium is also killed if the proxy dies. Profile folders left by a killed proxy are removed when the next one starts, once no running proxy holds them. |

### Agent page code (governed pages)

| Action | What Statelock does |
|---|---|
| `element.click()`, `dispatch_event("click")`, `dispatchEvent(...)` for click, dblclick, auxclick, submit, drop, paste | Cancelled before page handlers run. A plain left click from agent code (agent code running in that tab, or just finished with no input since) on a visible, uncovered element in the top frame is replayed as real mouse input at the element, governed and recorded (`params.statelock_replayed_script_click`). Statelock scrolls the element into view (`DOM.scrollIntoViewIfNeeded`), and sends each replayed event only if the element is still at its point; if the layout moved, it locates the element again and records the event as not sent (`statelock_replay_not_sent`). If it cannot safely complete the replay (the element moved after the press, or the replay failed), it ends the session (`synthetic_event`) rather than sending an unchecked release. Everything else is a violation (`synthetic_event`). The guard also covers the page's iframes (srcdoc, about:blank, other origins); one whose page cannot be identified is guarded (fail closed). |
| Reading cookies (`Storage.getCookies`, `Network.getCookies`, `Network.getAllCookies`; Playwright `context.cookies()` and `storage_state()`) | Answered by Statelock with only the cookies the policy's `cookie_access` allows (names recorded, values not); without `cookie_access`, declined (`cookie_export`). Before `Network.*` events reach the agent, Statelock redacts `Cookie`, `Set-Cookie` and authorization headers, raw header text and cookie-detail structures. The session continues. `page.request` / `context.request` are separate agent-side HTTP paths described below. Not covered: tokens a site keeps in `localStorage` / `sessionStorage` or in the page, which agent code can read. |
| Files put into a file input by code (`input.files = ...`, Playwright `set_input_files` over a remote connection) | Cleared before site handlers, form submission or `new FormData(form)` see them (`untrusted_file_input`). Checked on the synthetic input/change event and again before every real click, drop, paste and submit. |
| Page load, XHR, fetch or `sendBeacon` started by agent code | Failed (`agent_code_request`). Requests from agent code in an iframe of a governed page (also in its own process) are governed too. |
| The same, started indirectly: calling the site's functions, timers, promises, `async`/`await`, `eval`, `new Function`, inserted `<script>` elements | Failed (`agent_code_request`). |
| `form.submit()`, or `form.requestSubmit()` outside a real click, Enter key or touch | Failed (`untrusted_form_submission`). |
| Reading the page (`page.evaluate` without side effects) | Allowed. |

### Allowed and attributed

| Action | `initiated_by` in `network.jsonl` |
|---|---|
| Page loads and requests after real clicks or Enter, site redirects, site background requests | `site` |
| Agent `page.goto()`, reload, back/forward (the page load of the URL in the tab's history; if it cannot be read, the next page load within 1 s) | `agent_navigation`; code supplied in `Page.reload.scriptToEvaluateOnLoad` is still tagged `agent_code` |
| Agent code requests on non-governed pages | `agent_code` (allowed, logged) |

### Evidence

- per-action records (schema `"4"`, `statelock.audit.records`) with screenshots, captured state, verdict and post-verdict, written in sequence order through the `ArtifactSink`.
- redaction in the evidence: SSNs (with or without dashes or spaces), card-like numbers (grouped or not) that pass the Luhn check, and emails; injected secrets are removed by value.
- guard violations are action records (`Statelock.syntheticEvent`, `Statelock.agentCodeRequest`, `Statelock.untrustedFormSubmission`).
- guarded request log with attribution in `network.jsonl`.
- optional saved browser sessions encrypt cookies and localStorage with AES-256-GCM, scoped to tenant, agent and name. They are saved only after a clean session end, not after a violation. They do not include sessionStorage, IndexedDB or cross-origin iframe storage. This is session reuse, not an encrypted evidence archive.

## Not Analyzed

### Agent page code that Statelock cannot attribute

1. **Inline event-handler attributes in markup the agent inserts**, e.g. `innerHTML = '<img src=x onerror="fetch(...)">'`. The handler runs as site code, so its requests are labelled `site`.
2. **Requests from markup the agent inserts**, e.g. `<meta http-equiv=refresh>`, `<iframe src>` or `<img src>`. These are started by the HTML parser, not by script.
3. **Changing form values with code, then submitting with a real click.** The submit is real input and is allowed. Pre-conditions do see the changed values at click time.
4. **Changing the DOM through CDP** (`DOM.setAttributeValue`, `DOM.setOuterHTML`, `DOM.setNodeValue`). This is not intercepted.
5. **Requests with no initiator reported within 1 s.** These are allowed and logged as `unknown`.

### Request types not intercepted

6. Images, scripts, stylesheets, fonts, media and prefetch requests.
7. EventSource. Chromium rejects it as a Fetch interception type.
8. WebSocket messages and WebRTC.
9. Requests from web workers, shared workers and service workers. These run in separate targets that are not guarded.

### Agent protocol commands not analyzed

10. **Navigation commands** (`page.goto`, reload, back/forward). Web URLs are allowed without a navigation allowlist; non-web URLs supplied to `Page.navigate` and `Target.createTarget` are refused (`navigation_url`, except `about:blank`). Back/forward to a page loaded by a POST re-sends the POST. `Page.reload.scriptToEvaluateOnLoad` is tagged as agent code.
11. Clipboard, permissions and browser settings.
12. **Downloads.** Playwright's own `page.expect_download` works through Statelock only after `statelock.client.install()` (else use `governed.expect_download` or `statelock.client.expect_download`); `page.on("download")` listeners do not fire. A download started from an iframe is captured from the tab heuristically. Download contents are hashed, not stored with the evidence.
13. **Agent-side HTTP.** Without `install()`, `page.request` / `context.request` fail, because cookies are not exported (above). With `install()` (or `statelock_fetch`, JS `statelockFetch`) they become governed requests (`Statelock.fetch`, policy `request_access`): made in the page, checked, recorded. Same-origin or CORS-allowed URLs only; the response is capped at `max_response_bytes`. Redirects fail before the destination request is sent. The URL is put in the browser's form first; a URL Python and the browser could read differently (backslash, user info, control or non-ASCII characters, dot segments, non-standard IPv4 forms) is declined. At most `STATELOCK_MAX_CONCURRENT_REQUESTS` (4) run at once per session; more are declined (`request_concurrency`) and recorded. HTTP the agent sends from its own process with credentials it holds (not the browser's session) is outside Statelock; only network isolation of the agent stops it.
14. **Content of agent drag data.** `Input.dispatchDragEvent` carries data the agent supplies (text, URLs, file paths). It is recorded but not checked against what the page offered.
15. **Other agent network commands** such as `Network.emulateNetworkConditions` (offline, throttling) and `Network.setExtraHTTPHeaders`. These are not refused.
16. **Other debugger commands.** `Debugger.evaluateOnCallFrame` expressions are tagged as agent code, and `Debugger.setScriptSource` is refused. Breakpoint conditions, `Debugger.setVariableValue` and `Debugger.setReturnValue` can still run or change page code without the agent-code tag; they are not refused.

### File uploads

17. **Playwright's own `set_input_files` on governed pages.** Over `connect_over_cdp`, Playwright sets files with page code, so the guard clears them (fail closed). `statelock.client.install()` routes async Playwright through Statelock; `statelock.client.install_sync()` does the same for sync Playwright. Otherwise use `governed.set_input_files` (the session from `connect_playwright`) or `statelock.client.set_input_files(page, target, files)`.
18. **File inputs inside iframes** are not supported by `set_input_files` yet (main frame only).
19. **File inputs inside shadow roots.** The pre-submit check scans the document and the form's elements, so files set silently in a shadow-root input are not cleared. Code-set files that fire Playwright's composed `input` event are still caught.
20. **Uploaded file contents are not stored with the evidence.** Name, size and SHA-256 are recorded; the files are deleted when the session ends. Files set by code record names only.
21. **Upload rules check metadata only.** `restrict_uploads` checks name, extension, size and count, not file contents.

### Remembered values

- A remembered value is the latest one Statelock captured: at a governed action, at page load (and 0.5 s later), or before an agent navigation command. A value that changes on the page after the last capture, without any of these, is not seen.
- Fields are read in Statelock's own isolated world, as the policy defines them; markup with the same `data-statelock-key` name does not change a policy field.
- A field's `url_contains` (and a policy's `target_url_contains`) matches the page URL as `scheme://host[:port]/path`, without query, fragment or user info. Path-only scopes are substrings; absolute scopes require the exact origin and a path equal to the scope's path or below it. Remembered fields need an absolute scope or explicit `allowed_origins` so a value from another origin cannot be planted in memory.
- Each field reads the first element its selector matches in the main document (or its `frame`); lists, tables and shadow roots are not supported. `visible: true` counts elements that are rendered (not transparent, hidden or zero-sized), not left of or above the page; below the fold counts. `data-statelock-value` markup applies only to `data-statelock-key` fields.

### Policy and enforcement gaps

22. Space-key activation of a focused button runs pre-conditions but not post-conditions.
23. Scroll and pinch gestures do not get `prohibit_click_text` or post-conditions (touch events and tap gestures do).
24. After a guard violation, the agent sees the error on its next guarded call, not on the call that caused it.
25. On governed pages, the site's own `form.submit()` and `form.requestSubmit()` outside a user gesture (e.g. from a timer), synthetic clicks, drops and pastes, and files the site sets on file inputs with code, are also blocked (false positives). A site click made while agent code runs in the tab (or just after, before any input) is replayed as a real click instead (governed, but recorded as the agent's). A replayed click lands a moment after the agent's `element.click()` returns; the agent's next command waits for it (`STATELOCK_REPLAY_WAIT`, 10 s). `allow_synthetic_events: true` disables the guard for a policy.
26. With `STATELOCK_AUTH_MODE=none` (development only), agent identity is self-asserted and `/violations/<id>` is open. With API keys (the default), both are authenticated.
27. **Credential injection** (`{{secret:name}}`) replaces placeholders in `Input.insertText` and `Input.imeSetComposition` only; a placeholder typed key by key is sent literally. The value is removed from every in-band answer and `Statelock.fetch` response the agent receives and from the evidence, but agent page code can read a field and transform the value (reverse it, encode it) before returning it. Password fields are recognized as below.
28. **Secret fields** are recognized by `type=password` or `autocomplete` (`current-password`, `new-password`, `one-time-code`, `cc-number`, `cc-csc`, `cc-exp`). Secrets typed into other fields are only covered by the pattern redaction (SSNs with or without dashes or spaces, card-like numbers that pass the Luhn check, emails). Screenshots can still show what a page displays.
29. The guard is verified on Chromium 141 only.
30. **Visual checks** (`visual_assert`) see only the visible viewport of the screenshot, and a model can misread it or follow text on the page. They fail closed on any model problem, but a confident wrong answer passes unless `cross_check` or another rule catches it. By default only activations (press, tap, drop, Enter/Space) are checked in pre-conditions. Screenshots are sent to the configured model server.
31. **Long page text.** State capture keeps at most 1,000,000 characters of rendered body text. `prohibit_page_text` fails closed when that limit is exceeded or when parts of the page could not be read; `require_page_text` checks only the captured text. Text is compared after NFKC, case folding, removal of invisible format characters and whitespace normalization; `prohibit_page_text` also ignores spaces. The page text includes open shadow roots and same-origin iframes that are shown. Closed shadow roots are not read. Shown iframes Statelock cannot read (other origins) are listed in `page_text_unread`.
32. **Currency comparisons.** When both values name a currency, the currencies must be the same: any ISO 4217 code written in upper case next to the amount ("USD 5", "5 EUR", "(5.00 EUR)"; other words such as "Top up" or "ALL items" are not codes; a code in lower or mixed case next to the amount, such as "usd 5", makes the amount unparseable, so it compares only as text and `assert_compare` blocks), unambiguous symbols mapped to their code (`€` = `EUR`), prefixed dollars and yen (`US$`, `C$`, `A$`, `CN¥`, ...) mapped to their code, and `$`, `¥` or any other symbol each treated as a currency of its own. A currency-marked amount can still compare equal to a bare number; the rule does not infer the bare number's currency. Amounts are exact decimals; non-finite numbers do not parse.
