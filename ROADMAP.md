# Roadmap

What Statelock does today and what is planned next. [COVERAGE.md](https://github.com/inferlink/statelock/blob/main/COVERAGE.md) lists in detail what is and is not analyzed.

## Shipped in 0.1

- **CDP proxy.** A governed Chromium per session, with CDP over a pipe (no DevTools port) and its sandbox on in Docker; agents connect with an API key or a single-use session URL.
- **Governed input.** Mouse, keyboard, text, touch, drag and drop, IME and gestures: state capture, pre-conditions, post-conditions after commit actions.
- **Page guard.** Synthetic events, code-set files, `form.submit()` and requests started by agent page code are blocked; script clicks are replayed as governed real clicks.
- **Policies.** Fields by markup, selector or URL; remembered values across pages; text, equality, numeric, click-text, upload and download rules; triggers; custom rules (`statelock.rules` entry points or `STATELOCK_RULE_MODULES`).
- **Visual checks.** `visual_assert` with any vision model litellm supports, hosted or local, failing closed.
- **Human review.** `on_fail: review` pauses an action for a reviewer (web page and API).
- **Files.** Governed uploads and downloads, recorded with size and SHA-256.
- **Agent-side HTTP.** `request_access`: requests made in the page with the browser's session, checked and recorded.
- **Credentials.** Credential injection (`{{secret:name}}`) and encrypted saved browser sessions; cookies are not exported to the agent.
- **Evidence.** Per-action records, screenshots and a request log, with redaction, through a pluggable sink.
- **Clients.** Python SDK (Playwright async and sync), browser-use, LangChain and CrewAI tools, a JavaScript/TypeScript SDK, and Stagehand examples.

## Planned

- Playwright gaps: `page.on("download")` listeners and sync `page.request` through Statelock.
- Governed HTTP redirects with a destination check before each redirected request; redirects currently fail closed.
- The JavaScript SDK on npm, and Playwright JS `page.request` through Statelock (today: `statelockFetch`).
- CI coverage of every extra (the CrewAI conversion is not yet tested in CI).
- Structured logs, metrics and tracing; load tests for concurrent sessions.
- Durable storage for reviews, so pending reviews and their history survive a restart.
- Uploads into file inputs inside iframes.
- Upload and download rules on file contents (type sniffing, malware scanning), not only name, extension, size and count.
- Open questions: support for other CDP clients and protocols, such as Selenium and WebDriver BiDi.
