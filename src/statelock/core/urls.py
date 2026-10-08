# SPDX-License-Identifier: Apache-2.0
"""The part of a URL that URL scopes (``target_url_contains``, ``url_contains``) match."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def origin_and_path(url: str) -> tuple[str, str] | None:
    """(scheme://host[:port], path) of a URL; None when it does not parse.

    User info, query and fragment are left out: whoever builds the link picks them,
    so ``https://evil.example/?x=/bank`` or ``https://bank.example@evil.example/``
    never count as the bank's pages. Scheme and host are lowercased and a default
    port is dropped.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.netloc:  # about:blank, data:...
        return f"{parts.scheme}:", parts.path
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != DEFAULT_PORTS.get(parts.scheme):
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}", parts.path


def page_address(url: str) -> str | None:
    """scheme://host[:port]/path of a URL (see origin_and_path); None when it does not parse."""
    split = origin_and_path(url)
    return None if split is None else split[0] + split[1]


def url_in_scope(scope: str, url: str | None) -> bool:
    """Path-only scopes are substrings; absolute scopes require an exact origin."""
    if scope.startswith(("http://", "https://")):
        expected = origin_and_path(scope)
        actual = origin_and_path(url) if url else None
        if expected is None or actual is None or expected[0] != actual[0]:
            return False
        scoped_path = expected[1].rstrip("/")
        return not scoped_path or re.search(re.escape(scoped_path) + r"(?=/|$)", actual[1]) is not None
    address = page_address(url) if url else None
    return address is not None and scope in address
