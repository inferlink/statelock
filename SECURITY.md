# Security policy

## Reporting a vulnerability

Please do not open a public issue. Report it privately, in either of these ways:

- email [inquiry@inferlink.com](mailto:inquiry@inferlink.com) with "Statelock security" in the subject;
- use GitHub's private vulnerability reporting: [Report a vulnerability](https://github.com/inferlink/statelock/security/advisories/new) on the repository's Security tab.

## What to include

- The Statelock version, and how you run it (pip or Docker, Chromium version, agent framework).
- What an agent or a page can do that it should not: for example, an action that bypasses a policy, a secret that reaches the agent or the evidence, or a request Statelock does not attribute.
- Steps to reproduce: a policy, a page or script, and the agent code, as small as you can make them.
- The impact as you see it.

## What to expect

- We acknowledge your report, and keep you informed while we investigate and fix it.
- We agree with you when the issue is disclosed, after a fixed release is available.
- We credit you in the release notes unless you prefer not to be named.

## Supported versions

Security fixes go into the latest release. Please check that the issue still occurs there.
