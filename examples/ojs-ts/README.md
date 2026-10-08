# OJS screening agent in TypeScript (Stagehand v3)

This is a shorter TypeScript port of `examples/ojs`, on Stagehand v3 and `@statelock/client`. The agent:

- logs in without the password (it types `{{secret:ojs_password}}`);
- reuses its saved login on later runs;
- lists the active submissions;
- for each one, downloads the manuscript through Statelock and reads the comments to the editor;
- writes `results.json`.

`OJS_DEMO_PROHIBITED_CLICK="Send to Review"` makes it click a button the policy prohibits, and Statelock ends the session (exit 2).

```bash
(cd ../../js && npm install && npm run build)
npm install
# Statelock and the mock journal as in examples/ojs/README.md, then:
export STATELOCK_URL=http://localhost:8010 STATELOCK_API_KEY=slk_dev_ojs_screening_agent
export OJS_BASE_URL=http://ojs-mock:8081/index.php/journal OJS_USERNAME=editor
npm start
```

The Statelock parts in `ojs_agent.ts`:

- `createSessionUrl({ savedSession, saveSession })`;
- `session.guard()`;
- `secret()`;
- the download helpers (Stagehand v3 has no download API).

`tests/test_example_ojs_ts.py` runs this agent against the mock journal when the packages are installed.

Stagehand v4 is not supported: its driver runs as a browser extension, not over CDP.
