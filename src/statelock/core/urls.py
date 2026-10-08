# SPDX-License-Identifier: Apache-2.0
"""URL scopes (``target_url_contains``, ``url_contains``) and agent-supplied request URLs."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
# Only printable ASCII without spaces: a browser drops or rewrites tabs, newlines and
# other controls, and encodes the rest, so Python would match a URL the browser never loads.
_URL_CHARACTERS = re.compile(r"[\x21-\x7e]+")
_HOST = re.compile(r"[a-z0-9_.-]+")
_PORT = re.compile(r"[0-9]*")
# A browser reads these segments as "." or ".." and removes them from the path.
_DOT_SEGMENTS = {".", "..", "%2e", ".%2e", "%2e.", "%2e%2e"}
# What a browser percent-encodes in the path and in the query of an http(s) URL.
_PATH_ENCODED = {'"': "%22", "<": "%3C", ">": "%3E", "`": "%60", "{": "%7B", "}": "%7D"}
_QUERY_ENCODED = {'"': "%22", "<": "%3C", ">": "%3E", "'": "%27"}


class UrlError(ValueError):
    """An agent-supplied URL that Statelock and the browser could read differently."""


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


def path_in_scope(scope_path: str, path: str) -> bool:
    """The path is the scope's path or continues it after a "/" (``/login`` covers
    ``/login`` and ``/login/x``, not ``/loginfoo`` or ``/forum/login``)."""
    scoped = scope_path.rstrip("/")
    return not scoped or path == scoped or path.startswith(scoped + "/")


def url_in_scope(scope: str, url: str | None) -> bool:
    """Path-only scopes are substrings; absolute scopes require an exact origin and a path under the scope's."""
    if scope.startswith(("http://", "https://")):
        expected = origin_and_path(scope)
        actual = origin_and_path(url) if url else None
        if expected is None or actual is None or expected[0] != actual[0]:
            return False
        return path_in_scope(expected[1], actual[1])
    address = page_address(url) if url else None
    return address is not None and scope in address


def canonical_http_url(url: str) -> str:
    """The form of an agent-supplied http(s) URL that is checked, scoped and loaded.

    Python's parser and the browser's (WHATWG) disagree on some URLs, e.g. a
    backslash before an ``@``: ``https://a.example\\@b.example/`` is host
    ``a.example`` to Python and ``b.example`` to the browser. Such URLs are refused
    (UrlError): characters outside printable ASCII, backslashes, user info, IP
    addresses a browser would rewrite, dot segments and invalid ports. The rest is
    written as the browser writes it: lowercase scheme and host, no default port, a
    path, the characters a browser escapes percent-encoded, and no fragment (it is
    never sent).
    """
    if not _URL_CHARACTERS.fullmatch(url):
        raise UrlError("the URL may only contain printable ASCII characters (percent-encode the rest)")
    if "\\" in url:
        raise UrlError("the URL contains a backslash")
    scheme, separator, rest = url.partition("://")
    scheme = scheme.lower()
    if not separator or scheme not in {"http", "https"}:
        raise UrlError("the URL must start with http:// or https://")
    rest = rest.partition("#")[0]
    authority_end = min((index for index in (rest.find("/"), rest.find("?")) if index >= 0), default=len(rest))
    authority, remainder = rest[:authority_end], rest[authority_end:]
    if "@" in authority:
        raise UrlError("the URL contains user info (user@host)")
    host, port = _split_port(authority)
    path, query_mark, query = remainder.partition("?")
    path = path or "/"
    if any(segment.lower() in _DOT_SEGMENTS for segment in path.split("/")):
        raise UrlError("the URL path contains a . or .. segment")
    canonical = f"{scheme}://{_canonical_host(host)}"
    if port is not None and port != DEFAULT_PORTS[scheme]:
        canonical += f":{port}"
    canonical += _escape(path, _PATH_ENCODED)
    if query_mark:
        canonical += "?" + _escape(query, _QUERY_ENCODED)
    return canonical


def _split_port(authority: str) -> tuple[str, int | None]:
    if authority.startswith("["):
        host, bracket, after = authority.partition("]")
        if not bracket or (after and not after.startswith(":")):
            raise UrlError("the URL has an invalid IPv6 host")
        host, port = host + "]", after[1:] if after else ""
    else:
        host, _, port = authority.partition(":")
    if not _PORT.fullmatch(port) or (port and int(port) > 65535):
        raise UrlError("the URL has an invalid port")
    return host, int(port) if port else None


def _canonical_host(host: str) -> str:
    if host.startswith("["):
        if "%" in host:  # a zone id: no browser takes one
            raise UrlError("the URL has an IPv6 zone id")
        try:
            return f"[{_ipv6_text(ipaddress.IPv6Address(host[1:-1]))}]"
        except ValueError as error:
            raise UrlError("the URL has an invalid IPv6 host") from error
    host = host.lower()
    if not host or not _HOST.fullmatch(host):
        raise UrlError("the URL host may only contain letters, digits, '.', '-' and '_' (use punycode)")
    last = host.rstrip(".").rpartition(".")[2]
    # A browser reads a host ending in a number as an IPv4 address, in any of several
    # forms (127.1, 0x7f.0.0.1, 2130706433): only the plain dotted form is accepted.
    if last[:1].isdigit() and not _dotted_ipv4(host):
        raise UrlError("the URL host must be a name or a dotted IPv4 address (a.b.c.d)")
    return host


def _ipv6_text(address: ipaddress.IPv6Address) -> str:
    """As a browser writes it: eight hex pieces, the first longest run of two or more zero pieces as "::"."""
    pieces = [int.from_bytes(address.packed[i : i + 2], "big") for i in range(0, 16, 2)]
    best_start, best_length, start = -1, 1, None
    for index, piece in enumerate([*pieces, 1]):
        if piece == 0 and start is None:
            start = index
        elif piece != 0 and start is not None:
            if index - start > best_length:
                best_start, best_length = start, index - start
            start = None
    text = [format(piece, "x") for piece in pieces]
    if best_start < 0:
        return ":".join(text)
    return ":".join(text[:best_start]) + "::" + ":".join(text[best_start + best_length :])


def _dotted_ipv4(host: str) -> bool:
    try:
        return str(ipaddress.IPv4Address(host)) == host
    except ValueError:
        return False


def _escape(text: str, characters: dict[str, str]) -> str:
    return "".join(characters.get(character, character) for character in text)
