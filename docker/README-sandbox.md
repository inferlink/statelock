# Chromium sandbox in Docker

`compose.yaml` runs Chromium with its sandbox (`STATELOCK_CHROMIUM_SANDBOX=1`, the
compose default). Chromium's sandbox needs unprivileged user namespaces, which
Docker's default seccomp profile blocks, so `compose.yaml` uses
`docker/seccomp_profile.json`: Playwright's profile (Docker's default plus
user-namespace cloning), v1.56.0, Apache 2.0 (Microsoft). The image runs as the
non-root user `pwuser`.

Outside compose, Statelock's own default is `STATELOCK_CHROMIUM_SANDBOX=false`.

## Updating the profile

Keep it pinned to the Playwright version in the Dockerfile:

```bash
curl -fsSL -o docker/seccomp_profile.json \
  https://raw.githubusercontent.com/microsoft/playwright/v1.56.0/utils/docker/seccomp_profile.json
```

## Check

```bash
docker compose run --rm statelock statelock check-sandbox
# Chromium started (sandbox=on).
```

If the sandbox cannot start, Chromium exits and Statelock refuses the session
(fail closed); the error includes Chromium's own message ("No usable sandbox").

## Turning it off

On a host that cannot use the profile, set `STATELOCK_CHROMIUM_SANDBOX=0` in `.env`.
Do not use `--privileged` or `--cap-add=SYS_ADMIN` instead: both weaken the
container more than the profile does.
