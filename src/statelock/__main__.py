# SPDX-License-Identifier: Apache-2.0
"""`statelock` command.

statelock [--host H] [--port P]            run the proxy (default)
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
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from statelock.auth import DEFAULT_TENANT, KeysFile, generate_key, hash_key
from statelock.fileio import load_yaml, write_atomic


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
    before = KeysFile.model_validate(load_yaml(text) or {})
    updated = _insert_entry(text, section, yaml.safe_dump([entry], sort_keys=False))
    after = load_yaml(updated) or {}
    KeysFile.model_validate(after)  # also refuses a key that is already listed
    entries = after.get(section) or []
    if entry not in entries or len(entries) != len(getattr(before, section)) + 1:
        raise ValueError("the entry could not be added; add it by hand")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    write_atomic(path, updated.encode("utf-8"), mode=mode)


def _insert_entry(text: str, section: str, item: str) -> str:
    """``text`` with the YAML list ``item`` added to the top-level ``section`` list."""
    root = yaml.compose(text) if text.strip() else None
    if root is not None and not isinstance(root, MappingNode):
        raise ValueError("the keys file is not a mapping")
    pairs = root.value if root else []
    value = next((v for k, v in pairs if isinstance(k, ScalarNode) and k.value == section), None)
    if value is None:  # no such section yet: add it at the end
        prefix = text if not text or text.endswith("\n") else text + "\n"
        return f"{prefix}{section}:\n{_indent(item, '  ')}"
    if not isinstance(value, SequenceNode) or value.flow_style or not value.value:
        raise ValueError(f"{section} must be a block list (- entries) to append to; add the entry by hand")
    # Indent like the first entry; insert on the line after the last one (before any
    # comment that introduces the next section).
    first = value.value[0].start_mark
    line_start = text.rfind("\n", 0, first.index) + 1
    indent = text[line_start : first.index].partition("-")[0]
    if indent.strip() or "-" not in text[line_start : first.index]:
        raise ValueError(f"{section}: the first entry is not written as '- key: value'; add the entry by hand")
    line_end = text.find("\n", _content_end(value.value[-1]))
    if line_end < 0:
        return f"{text}\n{_indent(item, indent)}"
    return f"{text[: line_end + 1]}{_indent(item, indent)}{text[line_end + 1 :]}"


def _content_end(node: Node) -> int:
    """Where the text of a node ends (a block collection's own end mark lies past its trailing comments)."""
    while isinstance(node, (MappingNode, SequenceNode)) and not node.flow_style and node.value:
        last = node.value[-1]
        node = last[1] if isinstance(node, MappingNode) else last
    return int(node.end_mark.index)


def _indent(block: str, indent: str) -> str:
    return "".join(indent + line for line in block.splitlines(keepends=True))


def _session_url(args: argparse.Namespace) -> int:
    from statelock.client.sessions import SessionUrlError, create_session_url  # noqa: PLC0415

    try:
        url = create_session_url(
            args.server,
            agent_id=args.agent_id,
            ttl_seconds=args.ttl,
            saved_session=args.saved_session,
            save_session=args.save,
        )
    except SessionUrlError as error:
        print(error, file=sys.stderr)
        return 1
    print(url.ws_url if args.ws else url.cdp_url)
    print(f"session {url.session_id} for {url.agent_id}; single use, expires {url.expires_at}", file=sys.stderr)
    return 0


def _saved_sessions(args: argparse.Namespace) -> int:
    from statelock.client.sessions import (  # noqa: PLC0415
        SessionUrlError,
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
    except SessionUrlError as error:
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
    from statelock.proxy.browser import BrowserLaunchError, ChromiumCdpLauncher  # noqa: PLC0415 - heavy import
    from statelock.settings import Settings  # noqa: PLC0415

    settings = Settings()

    async def launch() -> None:
        browser = await ChromiumCdpLauncher(host=settings.chromium_host, sandbox=settings.chromium_sandbox).launch()
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
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
