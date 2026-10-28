# SPDX-License-Identifier: Apache-2.0
"""Governed HTTP requests: Statelock makes them in the page, with the browser's session.

    response = await statelock_fetch(page, "https://portal.example.com/api/invoices")
    invoices = await response.json()

After ``statelock.client.install()`` (or ``install_sync()``; ``statelock_fetch_sync``),
Playwright's own ``page.request`` and ``context.request`` (``get``, ``post``, ``put``,
``patch``, ``delete``, ``head``, ``fetch``) do this on a Statelock browser. The agent's policy must allow the URL
and method (``request_access``). A declined request raises
``StatelockRequestError``. The response works like Playwright's ``APIResponse``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.async_api import APIRequestContext, Page
from playwright.sync_api import APIRequestContext as SyncAPIRequestContext
from playwright.sync_api import Page as SyncPage

from statelock.client.files import cdp_session, statelock_session
from statelock.client.files_sync import cdp_session as cdp_session_sync
from statelock.client.files_sync import statelock_session_sync
from statelock.wire import REQUEST_COMMAND, VIOLATION_MARKER

R = TypeVar("R", bound="_Response")


class StatelockRequestError(Exception):
    """Statelock declined or could not make the request (the message says why)."""


class _Response:
    """What a governed request's response holds (attributes as in Playwright's APIResponse)."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.status: int = int(result.get("status") or 0)
        self.status_text: str = str(result.get("statusText") or "")
        self.url: str = str(result.get("url") or "")
        pairs = result.get("headers")
        self._headers = [(str(k), str(v)) for k, v in pairs] if isinstance(pairs, list) else []
        self.headers: dict[str, str] = {k.lower(): v for k, v in self._headers}
        self._body = base64.b64decode(str(result.get("body") or ""))

    @property
    def ok(self) -> bool:
        return 200 <= self.status <= 299

    def headers_array(self) -> list[dict[str, str]]:
        return [{"name": k, "value": v} for k, v in self._headers]

    def __repr__(self) -> str:
        return f"<{type(self).__name__} url={self.url!r} status={self.status}>"


class StatelockResponse(_Response):
    """The response of a governed request, with Playwright's async APIResponse API."""

    async def body(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    async def json(self) -> Any:
        return json.loads(self._body)

    async def dispose(self) -> None:
        return None


class StatelockResponseSync(_Response):
    """The response of a governed request, with Playwright's sync APIResponse API."""

    def body(self) -> bytes:
        return self._body

    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self._body)

    def dispose(self) -> None:
        return None


def _with_query(url: str, params: dict[str, Any] | str | None) -> str:
    if not params:
        return url
    query = params if isinstance(params, str) else urlencode({k: str(v) for k, v in params.items()})
    parts = urlsplit(url)
    joined = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, joined, parts.fragment))


def _encode_body(
    headers: dict[str, str], data: Any, form: dict[str, Any] | None
) -> tuple[bytes | None, dict[str, str]]:
    lowered = {k.lower() for k in headers}
    if form is not None:
        if "content-type" not in lowered:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return urlencode({k: str(v) for k, v in form.items()}).encode(), headers
    if data is None:
        return None, headers
    if isinstance(data, bytes):
        return data, headers
    if isinstance(data, str):
        return data.encode("utf-8"), headers
    if "content-type" not in lowered:
        headers["Content-Type"] = "application/json"
    return json.dumps(data).encode("utf-8"), headers


def _request_command(
    url: str,
    method: str,
    headers: dict[str, str] | None,
    data: Any,
    form: dict[str, Any] | None,
    params: dict[str, Any] | str | None,
) -> dict[str, Any]:
    """The ``Statelock.fetch`` parameters for a request."""
    body, request_headers = _encode_body(dict(headers or {}), data, form)
    return {
        "url": _with_query(url, params),
        "method": method.upper(),
        "headers": request_headers,
        "body": base64.b64encode(body).decode("ascii") if body is not None else None,
    }


def _checked(response: R, *, fail_on_status_code: bool) -> R:
    """The response, or StatelockRequestError for a non-2xx status with fail_on_status_code."""
    if fail_on_status_code and not response.ok:
        raise StatelockRequestError(f"{response.status} {response.status_text} for {response.url}")
    return response


async def statelock_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: Any = None,
    form: dict[str, Any] | None = None,
    params: dict[str, Any] | str | None = None,
    fail_on_status_code: bool = False,
) -> StatelockResponse:
    """Have Statelock make the request in the page (policy request_access).

    data: bytes, text, or a JSON-serializable value (sent as JSON). form: sent URL-encoded.
    Header values may use {{secret:name}} placeholders (credential injection).
    """
    command = _request_command(url, method, headers, data, form, params)
    async with cdp_session(page) as cdp:
        try:
            result = await cdp.send(REQUEST_COMMAND, command)
        except Exception as error:
            if VIOLATION_MARKER in str(error):
                raise  # the session ended: the guard turns it into StatelockPolicyViolationError
            raise StatelockRequestError(str(error)) from error
    return _checked(StatelockResponse(dict(result)), fail_on_status_code=fail_on_status_code)


def statelock_fetch_sync(
    page: SyncPage,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: Any = None,
    form: dict[str, Any] | None = None,
    params: dict[str, Any] | str | None = None,
    fail_on_status_code: bool = False,
) -> StatelockResponseSync:
    """``statelock_fetch`` for Playwright's sync API."""
    command = _request_command(url, method, headers, data, form, params)
    with cdp_session_sync(page) as cdp:
        try:
            result = cdp.send(REQUEST_COMMAND, command)
        except Exception as error:
            if VIOLATION_MARKER in str(error):
                raise  # the session ended: the guard turns it into StatelockPolicyViolationError
            raise StatelockRequestError(str(error)) from error
    return _checked(StatelockResponseSync(dict(result)), fail_on_status_code=fail_on_status_code)


_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "head": "HEAD"}
_UNSUPPORTED = ("multipart", "max_redirects", "max_retries", "ignore_https_errors")


def _fetch_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Playwright's request options as statelock_fetch arguments; refuses what Statelock cannot do."""
    unsupported = [name for name in _UNSUPPORTED if kwargs.get(name) not in (None, False)]
    if unsupported:
        raise StatelockRequestError(f"not supported through Statelock: {', '.join(unsupported)}")
    return {
        "headers": kwargs.get("headers"),
        "data": kwargs.get("data"),
        "form": kwargs.get("form"),
        "params": kwargs.get("params"),
        "fail_on_status_code": bool(kwargs.get("fail_on_status_code")),
    }


def _fetch_url(url_or_request: Any) -> str:
    if not isinstance(url_or_request, str):
        raise StatelockRequestError("page.request.fetch through Statelock takes a URL string")
    return url_or_request


class GovernedRequestContext:
    """What ``page.request`` / ``context.request`` return after install(): governed requests
    on a Statelock browser, Playwright's own APIRequestContext elsewhere."""

    def __init__(self, page_of: Callable[[], Page | None], original: APIRequestContext) -> None:
        self._page_of = page_of
        self._original = original

    async def _governed_page(self) -> Page | None:
        page = self._page_of()
        return page if page is not None and await statelock_session(page) else None

    async def fetch(self, url_or_request: Any, **kwargs: Any) -> Any:
        page = await self._governed_page()
        if page is None:
            return await self._original.fetch(url_or_request, **kwargs)
        method = kwargs.pop("method", None) or "GET"
        return await statelock_fetch(page, _fetch_url(url_or_request), method=method, **_fetch_options(kwargs))

    def __getattr__(self, name: str) -> Any:
        method = _METHODS.get(name)
        original = getattr(self._original, name)
        if method is None:
            return original

        async def call(url: str, **kwargs: Any) -> Any:
            page = await self._governed_page()
            if page is None:
                return await original(url, **kwargs)
            return await statelock_fetch(page, url, method=method, **_fetch_options(kwargs))

        return call


class GovernedRequestContextSync:
    """What ``page.request`` / ``context.request`` return after install_sync(): governed
    requests on a Statelock browser, Playwright's own sync APIRequestContext elsewhere."""

    def __init__(self, page_of: Callable[[], SyncPage | None], original: SyncAPIRequestContext) -> None:
        self._page_of = page_of
        self._original = original

    def _governed_page(self) -> SyncPage | None:
        page = self._page_of()
        return page if page is not None and statelock_session_sync(page) else None

    def fetch(self, url_or_request: Any, **kwargs: Any) -> Any:
        page = self._governed_page()
        if page is None:
            return self._original.fetch(url_or_request, **kwargs)
        method = kwargs.pop("method", None) or "GET"
        return statelock_fetch_sync(page, _fetch_url(url_or_request), method=method, **_fetch_options(kwargs))

    def __getattr__(self, name: str) -> Any:
        method = _METHODS.get(name)
        original = getattr(self._original, name)
        if method is None:
            return original

        def call(url: str, **kwargs: Any) -> Any:
            page = self._governed_page()
            if page is None:
                return original(url, **kwargs)
            return statelock_fetch_sync(page, url, method=method, **_fetch_options(kwargs))

        return call
