"""Agent request URLs in the browser's form, and absolute URL scopes anchored at the path start."""

from __future__ import annotations

import pytest

from statelock.core.urls import UrlError, canonical_http_url, path_in_scope, url_in_scope
from statelock.proxy.requests import FetchRequest, RequestError


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("https://api.example/x", "https://api.example/x"),
        ("HTTPS://API.Example:443/x?q=1#frag", "https://api.example/x?q=1"),
        ("http://api.example:80", "http://api.example/"),
        ("http://api.example:08080/x", "http://api.example:8080/x"),
        ("https://api.example?q", "https://api.example/?q"),
        ('https://api.example/a"b<c>{d}`?q="x"', "https://api.example/a%22b%3Cc%3E%7Bd%7D%60?q=%22x%22"),
        ("https://10.0.0.1:8443/x", "https://10.0.0.1:8443/x"),
        ("https://[::FFFF:1.2.3.4]/x", "https://[::ffff:102:304]/x"),
        ("https://api.example/a%2Fb/%2e%2e%2f", "https://api.example/a%2Fb/%2e%2e%2f"),
    ],
)
def test_request_urls_are_written_as_the_browser_writes_them(url: str, canonical: str) -> None:
    assert canonical_http_url(url) == canonical


@pytest.mark.parametrize(
    "url",
    [
        # Python: host api.bank.example (after the "@"); the browser: api.public.example.
        "https://api.public.example\\@api.bank.example/x",
        "https://api.bank.example\\x",
        "https://user:pw@api.example/",
        "https://api.public.example@api.bank.example/",
        "https://api.example/a\tb",
        "https://api.ex\nample/",
        " https://api.example/",
        "https://api.example/\x00",
        "https://api.example/é",
        "https://\u0430pi.example/",  # a Cyrillic letter: hosts must be punycode
        "https://api.example/a/../admin",
        "https://api.example/a/%2e%2E/admin",
        "https://api.example/./x",
        "https://127.1/",
        "https://0x7f.0.0.1/",
        "https://2130706433/",
        "https://010.0.0.1/",
        "https://api.example.123/",
        "https://api.example:99999/",
        "https://api.example:8o/",
        "https://api%2eexample/",
        "https://[::1/",
        "https://[fe80::1%25eth0]/",
        "https:///x",
        "https:/api.example/",
        "ftp://api.example/",
        "javascript://api.example/%0aalert(1)",
    ],
)
def test_ambiguous_request_urls_are_refused(url: str) -> None:
    with pytest.raises(UrlError):
        canonical_http_url(url)
    with pytest.raises(RequestError):
        FetchRequest.parse({"url": url})


def test_fetch_request_carries_the_canonical_url() -> None:
    assert FetchRequest.parse({"url": "HTTPS://API.example:443/x"}).url == "https://api.example/x"


@pytest.mark.parametrize(
    ("url", "inside"),
    [
        ("https://bank.example/login", True),
        ("https://bank.example/login/", True),
        ("https://bank.example/login/step-2", True),
        ("https://bank.example/login?next=/x", True),
        ("https://bank.example/loginfoo", False),
        ("https://bank.example/forum/posts/login", False),
        ("https://bank.example/x/login/y", False),
        ("https://evil.example/login", False),
        ("http://bank.example/login", False),
    ],
)
def test_absolute_scope_paths_are_anchored_at_the_start(url: str, *, inside: bool) -> None:
    assert url_in_scope("https://bank.example/login", url) is inside
    assert url_in_scope("https://bank.example/login/", url) is inside


def test_an_origin_scope_covers_every_path_of_that_origin() -> None:
    assert url_in_scope("https://bank.example", "https://bank.example/any/path")
    assert url_in_scope("https://bank.example/", "https://bank.example/")
    assert path_in_scope("/", "/x") and path_in_scope("", "/x")
