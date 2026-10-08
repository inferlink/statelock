# SPDX-License-Identifier: Apache-2.0
"""`statelock` command.

statelock [--host H] [--port P]            run the proxy (default: 127.0.0.1:8010)
statelock keygen --agent-id ID [--tenant T] [--append FILE]  new API key and its keys-file entry
                                           (or --reviewer / --auditor ID)
statelock session-url [--saved-session NAME [--save]]  single-use URL for a governed session
statelock saved-sessions list | delete NAME  the agent's saved browser sessions
statelock hash-key < key.txt               SHA-256 of a key read from stdin
statelock check-sandbox                    launch Chromium with the configured sandbox setting
statelock perception-check IMAGE -i TEXT [-x name=type]  try the configured vision model on a screenshot
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import stat
import sys
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from statelock.auth import DEFAULT_TENANT, KeysFile, generate_key, hash_key
from statelock.fileio import load_yaml, write_atomic

DEFAULT_PORT = 8010  # the port the README and the examples use


def _run(args: argparse.Namespace) -> int:
    uvicorn.run("statelock.app:create_app", factory=True, host=args.host, port=args.port)
    return 0


def _keygen(args: argparse.Namespace) -> int:
    key = generate_key()
    if args.reviewer:
        section, id_field, principal = "reviewers", "reviewer_id", args.reviewer
    elif args.auditor:
        section, id_field, principal = "auditors", "auditor_id", args.auditor
    else:
        section, id_field, principal = "agents", "agent_id", args.agent_id
    entry = {id_field: principal, "tenant": args.tenant, "key_sha256": hash_key(key)}
    if args.append is not None:
        try:
            append_key_entry(Path(args.append), section, entry)
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f"Could not add the key to {args.append}: {error}", file=sys.stderr)
            return 1
    # The key goes to stderr so the keys-file entry on stdout can be redirected safely.
    print(f"API key for {principal} (store it as a secret; it is not shown again):", file=sys.stderr)
    print(key, file=sys.stderr)
    if args.append is not None:
        print(f"Added {id_field} {principal} to {section} in {args.append}", file=sys.stderr)
    else:
        print(yaml.safe_dump({section: [entry]}, sort_keys=False), end="")
    return 0


def append_key_entry(path: Path, section: str, entry: dict[str, Any]) -> None:
    """Add an entry to a section of a keys file (created when missing), keeping the rest
    of the file as written, comments included. The result must be a valid keys file;
    it replaces the old one atomically."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    before = load_yaml(text) or {}
    KeysFile.model_validate(before)
    updated = _insert_entry(text, section, yaml.safe_dump([entry], sort_keys=False))
    after = load_yaml(updated) or {}
    KeysFile.model_validate(after)  # also refuses a key that is already listed
    # Exactly one change: the entry appended to its section.
    if {**after, section: None} != {**before, section: None} or after.get(section) != [
        *(before.get(section) or []),
        entry,
    ]:
        raise ValueError("the entry could not be added; add it by hand")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    write_atomic(path, updated.encode("utf-8"), mode=mode)


def _insert_entry(text: str, section: str, item: str) -> str:
    """``text`` with the YAML list ``item`` added at the end of the top-level ``section`` list,
    or a new section at the end. Plain text insertion: the caller parses the result and checks
    that it holds exactly the one new entry."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{section}:")), None)
    if start is None:
        return "".join(lines) + f"{section}:\n{_indent(item, '  ')}"
    value = lines[start][len(section) + 1 :].strip()
    if value and not value.startswith("#"):
        raise ValueError(f"{section} must be a block list (- entries) to append to; add the entry by hand")
    # The section runs until the next top-level key; a comment after its last entry (one that
    # introduces the next section) stays after the new entry.
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #-"):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    entries = [line for line in lines[start + 1 : end] if line.lstrip().startswith("-")]
    indent = entries[0][: len(entries[0]) - len(entries[0].lstrip())] if entries else "  "
    return "".join([*lines[:end], _indent(item, indent), *lines[end:]])


def _indent(block: str, indent: str) -> str:
    return "".join(indent + line for line in block.splitlines(keepends=True))


def _session_url(args: argparse.Namespace) -> int:
    from statelock.client.sessions import StatelockClientError, create_session_url  # noqa: PLC0415

    try:
        url = create_session_url(
            args.server,
            agent_id=args.agent_id,
            ttl_seconds=args.ttl,
            saved_session=args.saved_session,
            save_session=args.save,
        )
    except StatelockClientError as error:
        print(error, file=sys.stderr)
        return 1
    print(url.ws_url if args.ws else url.cdp_url)
    print(f"session {url.session_id} for {url.agent_id}; single use, expires {url.expires_at}", file=sys.stderr)
    return 0


def _saved_sessions(args: argparse.Namespace) -> int:
    from statelock.client.sessions import (  # noqa: PLC0415
        StatelockClientError,
        delete_saved_session,
        list_saved_sessions,
    )

    try:
        if args.action == "delete":
            if not args.name:
                print("delete needs a saved-session name", file=sys.stderr)
                return 2
            deleted = delete_saved_session(args.name, args.server, agent_id=args.agent_id)
            print("deleted" if deleted else "not found", file=sys.stderr)
            return 0 if deleted else 1
        for name in list_saved_sessions(args.server, agent_id=args.agent_id):
            print(name)
    except StatelockClientError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _hash_key(_args: argparse.Namespace) -> int:
    key = sys.stdin.read().strip()
    if not key:
        print("no key on stdin", file=sys.stderr)
        return 1
    print(hash_key(key))
    return 0


def _check_sandbox(_args: argparse.Namespace) -> int:
    from statelock.proxy.browser import BrowserLaunchError  # noqa: PLC0415 - heavy import
    from statelock.services import launcher  # noqa: PLC0415
    from statelock.settings import Settings  # noqa: PLC0415

    settings = Settings()

    async def launch() -> None:
        browser = await launcher(settings).launch()
        await browser.close()

    try:
        asyncio.run(launch())
    except BrowserLaunchError as error:
        print(f"Chromium failed to start (sandbox={settings.chromium_sandbox}): {error}", file=sys.stderr)
        return 1
    print(f"Chromium started (sandbox={'on' if settings.chromium_sandbox else 'off'}).")
    return 0


def _perception_check(args: argparse.Namespace) -> int:
    from statelock.core.state import BrowserState  # noqa: PLC0415
    from statelock.policy.perception import PerceptionRequest  # noqa: PLC0415
    from statelock.services import perception_evaluator  # noqa: PLC0415
    from statelock.settings import Settings  # noqa: PLC0415

    extract: dict[str, str] = {}
    for item in args.extract:
        name, _, kind = item.partition("=")
        extract[name] = kind or "string"
    try:
        evaluator = perception_evaluator(Settings())
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    if evaluator is None:
        print("Set STATELOCK_PERCEPTION_MODEL (and STATELOCK_PERCEPTION_API_BASE for a local model).", file=sys.stderr)
        return 2
    image = base64.b64encode(Path(args.image).read_bytes()).decode("ascii")
    try:
        request = PerceptionRequest(
            check_name="cli", instruction=args.instruction, extract=extract, state=BrowserState(screenshot_base64=image)
        )
    except ValueError as error:
        print(f"Bad --extract: {error}", file=sys.stderr)
        return 2
    verdict = asyncio.run(evaluator.evaluate(request))
    print(json.dumps(verdict.model_dump(), indent=2))
    return 0 if verdict.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="statelock", description="Statelock proxy and tools.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="address the proxy listens on (default: 127.0.0.1, this machine only)"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port the proxy listens on (default: {DEFAULT_PORT})"
    )
    parser.set_defaults(handler=_run)
    commands = parser.add_subparsers(title="commands")

    keygen = commands.add_parser("keygen", help="create an API key and its keys-file entry")
    principal = keygen.add_mutually_exclusive_group(required=True)
    principal.add_argument("--agent-id")
    principal.add_argument("--reviewer", metavar="REVIEWER_ID", help="create a reviewer key instead")
    principal.add_argument("--auditor", metavar="AUDITOR_ID", help="create an auditor key (evidence access)")
    keygen.add_argument("--tenant", default=DEFAULT_TENANT)
    keygen.add_argument(
        "--append", metavar="FILE", help="add the entry to this keys file (created if missing) instead of printing it"
    )
    keygen.set_defaults(handler=_keygen)

    session = commands.add_parser(
        "session-url", help="print a single-use URL that opens a governed session (key: STATELOCK_API_KEY)"
    )
    session.add_argument("--server", help="Statelock URL (default: STATELOCK_URL)")
    session.add_argument("--agent-id", help="only when the proxy's authentication is off")
    session.add_argument("--ttl", type=int, help="seconds the URL stays usable (default: the server's, 300)")
    session.add_argument("--ws", action="store_true", help="print the WebSocket URL instead of the http one")
    session.add_argument("--saved-session", metavar="NAME", help="restore this saved browser session")
    session.add_argument("--save", action="store_true", help="save the browser state under NAME at a clean end")
    session.set_defaults(handler=_session_url)

    saved = commands.add_parser("saved-sessions", help="list or delete the agent's saved browser sessions")
    saved.add_argument("action", choices=["list", "delete"])
    saved.add_argument("name", nargs="?")
    saved.add_argument("--server", help="Statelock URL (default: STATELOCK_URL)")
    saved.add_argument("--agent-id", help="only when the proxy's authentication is off")
    saved.set_defaults(handler=_saved_sessions)
    commands.add_parser("hash-key", help="print the SHA-256 of a key read from stdin").set_defaults(handler=_hash_key)
    commands.add_parser("check-sandbox", help="start Chromium with the configured sandbox").set_defaults(
        handler=_check_sandbox
    )

    perception = commands.add_parser(
        "perception-check", help="ask the configured vision model (STATELOCK_PERCEPTION_MODEL) about a screenshot"
    )
    perception.add_argument("image", help="a JPEG screenshot")
    perception.add_argument("-i", "--instruction", required=True, help="the check, e.g. 'The amounts match.'")
    perception.add_argument(
        "-x", "--extract", action="append", default=[], metavar="NAME=TYPE", help="a value to read (string, number...)"
    )
    perception.set_defaults(handler=_perception_check)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
