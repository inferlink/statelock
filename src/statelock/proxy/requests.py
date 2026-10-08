# SPDX-License-Identifier: Apache-2.0
"""Governed HTTP requests for the agent (``Statelock.fetch``, policy ``request_access``).

The agent sends ``Statelock.fetch`` {url, method, headers, body (base64)} on a page's
CDP session. Statelock checks it against the agent's ``request_access`` and, if
allowed, makes it with ``fetch()`` in an isolated world of that page, as the page's
own code would. The browser's cookies go with it, but the agent never receives
them. Same-origin and CORS-allowed URLs only, as for any page request.

- **Recorded.** Every request is an action record (URL, method, header names,
  body size, status, response size and SHA-256), and it appears in the network
  log as ``statelock_request``.
- **Declined.** A request no rule allows is answered with an error and recorded
  (rule ``request_access``). The session continues. Redirects fail before a
  request to their destination is sent.
- **Secrets.** Header values may carry ``{{secret:name}}`` placeholders (for
  example an API token). The secrets file must allow the agent, the request URL
  (``url_contains``) and non-password fields (``password_fields_only: false``).
  A placeholder that is not allowed ends the session (rule ``secret_injection``),
  as in typed input. Every secret the session injected is removed from
  the response.
- **Concurrency.** Requests run beside the agent's other commands, so a slow one
  does not hold up the session.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from statelock.core.enums import SystemRule
from statelock.core.jsonutil import as_dict, as_str
from statelock.core.verdict import PolicyVerdict
from statelock.credentials import InjectionError, SecretScrubber, SecretStore
from statelock.policy.requests import RequestAccess
from statelock.proxy.attribution import REQUEST_SOURCE_URL
from statelock.proxy.connection import CdpError
from statelock.proxy.pages import PageSessions
from statelock.proxy.targets import TargetRegistry
from statelock.proxy.tasks import BackgroundTasks
from statelock.proxy.worlds import REQUEST_WORLD, IsolatedWorld
from statelock.wire import statelock_error

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
EVALUATE_TIMEOUT = REQUEST_TIMEOUT_SECONDS + 5

# Runs in Statelock's isolated world of the page: the site's own fetch() is untouched
# there, and the sourceURL attributes the request to Statelock in the network log.
_FETCH = """
async ({url, method, headers, body, maxBytes, timeoutMs, redirect}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const init = {method, headers, credentials: "include", redirect, signal: controller.signal};
    if (body !== null) init.body = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
    const response = await fetch(url, init);
    const reader = response.body ? response.body.getReader() : null;
    const chunks = [];
    let size = 0;
    while (reader) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > maxBytes) { controller.abort(); return {error: "response_too_large", size}; }
      chunks.push(value);
    }
    let binary = "";
    for (const chunk of chunks) {
      for (let i = 0; i < chunk.length; i += 32768) binary += String.fromCharCode(...chunk.subarray(i, i + 32768));
    }
    return {
      status: response.status,
      statusText: response.statusText,
      url: response.url,
      redirected: response.redirected,
      headers: [...response.headers.entries()],
      body: btoa(binary),
    };
  } catch (error) {
    return {error: "fetch_failed", message: String(error && error.message || error)};
  } finally {
    clearTimeout(timer);
  }
}
"""


class RequestError(Exception):
    """A malformed request (answered as an invalid-params error, not recorded as a decision)."""


@dataclass(frozen=True)
class FetchRequest:
    url: str
    method: str
    headers: dict[str, str]
    body: str | None  # base64
    size: int = 0  # decoded body bytes

    @classmethod
    def parse(cls, params: dict[str, Any]) -> FetchRequest:
        url = params.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RequestError("Statelock.fetch needs an http(s) url")
        method = str(params.get("method") or "GET").upper()
        raw_headers = params.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise RequestError("Statelock.fetch headers must be an object")
        body = params.get("body")
        if body is not None and not isinstance(body, str):
            raise RequestError("Statelock.fetch body must be base64 text")
        size = 0
        if body:
            if len(body) * 3 // 4 > MAX_REQUEST_BODY_BYTES:
                raise RequestError(f"Statelock.fetch body is larger than {MAX_REQUEST_BODY_BYTES} bytes")
            try:
                size = len(base64.b64decode(body, validate=True))
            except (binascii.Error, ValueError) as error:
                raise RequestError("Statelock.fetch body is not valid base64") from error
        return cls(url, method, {str(k): str(v) for k, v in raw_headers.items()}, body or None, size)

    def summary(self) -> dict[str, Any]:
        """For the evidence: no header values, no body (they may carry credentials or data)."""
        return {"url": self.url, "method": self.method, "header_names": sorted(self.headers), "body_bytes": self.size}


@dataclass
class FetchOutcome:
    body: dict[str, Any]  # the CDP response for the agent
    verdict: PolicyVerdict
    recorded: dict[str, Any]  # the params to record
    ends_session: bool = False


class GovernedRequests:
    """Answers Statelock.fetch for one session."""

    def __init__(
        self,
        pages: PageSessions,
        targets: TargetRegistry,
        access: RequestAccess,
        *,
        agent_id: str,
        secrets: SecretStore,
        scrubber: SecretScrubber,
    ) -> None:
        self.pages = pages
        self.targets = targets
        self.access = access
        self.agent_id = agent_id
        self.secrets = secrets
        self.scrubber = scrubber
        self.tasks = BackgroundTasks("governed requests")
        self._world = IsolatedWorld(pages, REQUEST_WORLD)

    async def close(self) -> None:
        await self.tasks.close()

    async def answer(self, payload: dict[str, Any]) -> FetchOutcome:
        try:
            request = FetchRequest.parse(as_dict(payload.get("params")))
        except RequestError as error:
            return FetchOutcome(statelock_error(str(error)), _declined(str(error), {}), {})
        recorded = request.summary()
        rule = self.access.rule_for(request.method, request.url)
        if rule is None:
            reason = (
                f"Statelock does not make {request.method} {request.url} for this agent: "
                "no request_access rule in its policy allows it."
            )
            return FetchOutcome(statelock_error(reason), _declined(reason, recorded), recorded)
        try:
            headers, secret_names = self._inject(request)
        except InjectionError as error:
            reason = f"Blocked a secret placeholder in a request header: {error}"
            verdict = PolicyVerdict.block(reason=reason, rule=SystemRule.SECRET_INJECTION.value, evidence=recorded)
            return FetchOutcome(statelock_error(reason), verdict, recorded, ends_session=True)
        if secret_names:
            recorded["statelock_secrets"] = secret_names
        result = await self._fetch(payload, request, headers, rule.max_response_bytes)
        if "error" in result:
            reason = f"Statelock.fetch failed: {result.get('error')} {result.get('message') or ''}".strip()
            recorded["error"] = result.get("error")
            return FetchOutcome(statelock_error(reason), PolicyVerdict.allow(reason, recorded), recorded)
        final_url = str(result.get("url") or request.url)
        recorded.update(status=result.get("status"), final_url=final_url)
        if final_url != request.url and self.access.rule_for(request.method, final_url) is None:
            reason = f"Statelock.fetch was redirected to {final_url}, which no request_access rule allows."
            return FetchOutcome(statelock_error(reason), _declined(reason, recorded), recorded)
        body = base64.b64decode(str(result.get("body") or ""))
        recorded.update(response_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest())
        if self.scrubber:  # an endpoint may echo a header, or a value typed earlier
            result = {
                **result,
                "body": base64.b64encode(self.scrubber.bytes(body)).decode("ascii"),
                "headers": self.scrubber.data(result.get("headers")),
            }
        verdict = PolicyVerdict.allow(f"Request allowed by request_access ({rule.url_pattern})", recorded)
        return FetchOutcome({"result": result}, verdict, recorded)

    def _inject(self, request: FetchRequest) -> tuple[dict[str, str], list[str]]:
        headers: dict[str, str] = {}
        names: list[str] = []
        for name, raw in request.headers.items():
            value = raw
            if SecretStore.placeholders(raw):
                value, used = self.secrets.inject(raw, agent_id=self.agent_id, url=request.url, password_field=False)
                names.extend(used)
                for secret in used:
                    self.scrubber.add(self.secrets.value(secret))
            headers[name] = value
        return headers, sorted(set(names))

    async def _fetch(
        self, payload: dict[str, Any], request: FetchRequest, headers: dict[str, str], max_bytes: int
    ) -> dict[str, Any]:
        try:
            target_id = self.targets.target_for(as_str(payload.get("sessionId"))) or await self._first_page()
        except CdpError as error:
            return {"error": "fetch_failed", "message": str(error)}
        if target_id is None:
            return {"error": "no_page", "message": "Statelock.fetch needs an open page"}
        arguments = {
            "url": request.url,
            "method": request.method,
            "headers": headers,
            "body": request.body,
            "maxBytes": max_bytes,
            "timeoutMs": int(REQUEST_TIMEOUT_SECONDS * 1000),
            "redirect": "error",
        }
        expression = f"({_FETCH})({json.dumps(arguments)})\n//# sourceURL={REQUEST_SOURCE_URL}\n"
        params = {"expression": expression, "awaitPromise": True, "returnByValue": True}
        try:
            # Not retried after a timeout or a lost context: the request may have been sent.
            evaluated = await self._world.evaluate(target_id, params, timeout=EVALUATE_TIMEOUT)
        except CdpError as error:
            return {"error": "fetch_failed", "message": str(error)}
        value = as_dict(as_dict(evaluated.get("result")).get("value"))
        return value or {"error": "fetch_failed", "message": "no result"}

    async def _first_page(self) -> str | None:
        for target in await self.pages.list_targets():
            if target.get("type") == "page" and target.get("targetId"):
                return str(target["targetId"])
        return None


def _declined(reason: str, evidence: dict[str, Any]) -> PolicyVerdict:
    return PolicyVerdict.block(reason=reason, rule=SystemRule.REQUEST_ACCESS.value, evidence=evidence)
