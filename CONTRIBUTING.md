# Contributing to Statelock

Thanks for helping. Statelock is Apache-2.0 licensed and maintained by InferLink Corporation.

## Sign your commits (DCO)

Every commit in a pull request needs a `Signed-off-by` line. By adding it you certify the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): that you wrote the change, or otherwise
have the right to submit it under the project's license. There is no agreement to sign, only the line.

```bash
git commit -s -m "Explain what changed"     # adds: Signed-off-by: Your Name <you@example.com>
```

The name and email must match the commit's author. A check on every pull request refuses commits without it.

Forgot it?

```bash
git commit --amend -s --no-edit            # the last commit
git rebase --signoff main                  # every commit on your branch
git push --force-with-lease
```

## Before you open a pull request

```bash
pip install -e ".[dev]"
python -m playwright install chromium
ruff check src tests examples && ruff format --check src tests examples
python -m mypy
python -m pytest -q
```

Or with Docker: `docker compose -f compose.yaml -f compose.dev.yaml --profile test run --rm statelock-tests`.

- Keep changes focused, with tests for new behaviour and for each bug fixed.
- A change to interception, attribution or the page guard updates [COVERAGE.md](COVERAGE.md).
- A change to an extension point (the `ArtifactSink` protocol, the action record schema, events, plugin APIs) says so in
  the pull request: plugins depend on them.

## Security issues

Please do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md) for how to report it privately.
