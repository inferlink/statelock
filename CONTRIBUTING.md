# Contributing to Statelock

Thanks for helping. Statelock is Apache-2.0 licensed and maintained by InferLink Corporation.

## Sign your commits (DCO)

Every commit in a pull request needs a `Signed-off-by` line. By adding it you certify the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): that you wrote the change, or otherwise
have the right to submit it under the project's license. There is no agreement to sign, only the line.

```bash
git commit -s -m "Explain what changed"     # adds: Signed-off-by: Your Name <you@example.com>
```

The name and email must match the commit's author. The DCO workflow (`.github/workflows/dco.yml`) checks every commit of a pull request and fails if one has no matching line. Merge commits and bot commits (author name ending in `[bot]`) are skipped. The Release workflow runs the same check on every commit since the last release tag.

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

These are the checks CI runs (`.github/workflows/ci.yml`). CI also sets `STATELOCK_REQUIRE_BROWSER=1`, so a browser test fails instead of skipping when Chromium is missing, and runs the extras' tests and the JavaScript SDK tests (see [js/README.md](js/README.md#development)).

Or with Docker:

```bash
cp .env.example .env   # compose.yaml reads it
docker compose -f compose.yaml -f compose.dev.yaml --profile test run --rm statelock-tests
```

- Keep changes focused, with tests for new behaviour and for each bug fixed.
- A change to interception, attribution or the page guard updates [COVERAGE.md](COVERAGE.md).
- A change to an extension point (the `ArtifactSink` protocol, the action record schema, events, plugin APIs) says so in
  the pull request: plugins depend on them.

## Releases

Maintainers release from GitHub: Actions > Release > Run workflow, on `main` (`.github/workflows/release.yml`). Bump `__version__` in `src/statelock/__init__.py` on `main` first.

- The workflow runs only on `main`, and only when started by hand.
- It runs CI and the DCO check (commits since the last release tag) first.
- It builds the package and reads the version from the wheel. A version that already has a `v<version>` tag is refused.
- It publishes `statelock-ai` to PyPI with trusted publishing, through the `pypi` environment (set it up with required reviewers and `main` as its only deployment branch). No tokens are stored.
- It then tags `v<version>` and creates the GitHub release with the built files (a pre-release for a pre-release version).

## Security issues

Please do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md) for how to report it privately.
