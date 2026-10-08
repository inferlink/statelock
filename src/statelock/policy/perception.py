# SPDX-License-Identifier: Apache-2.0
"""Visual policy checks (rule ``visual_assert``): a vision-language model judges the screenshot.

The rule asks a yes/no question about the page Statelock captured, and can ask the
model to read values off the screenshot (``extract``). The model must answer with
strict JSON: ``{"passed": bool, "reason": str, "extracted": {...}}``. The output is
validated against that schema. Anything else fails closed: no evaluator configured,
no screenshot, a timeout, a model error, or output that does not match (after
``retries`` more attempts).

``LiteLLMPerceptionEvaluator`` calls any model litellm supports. For a local model:

- vLLM: ``hosted_vllm/Qwen/Qwen2.5-VL-7B-Instruct`` with ``api_base`` ``http://gpu:8000/v1``;
- Ollama: ``ollama_chat/qwen2.5vl:7b``.

Configure it with ``STATELOCK_PERCEPTION_MODEL`` (and ``..._API_BASE``, ``..._API_KEY``,
``..._TIMEOUT``, ``..._RETRIES``). Install the extra: ``pip install "statelock-ai[vlm]"``.

A hosted model receives a screenshot of the page at every checked action. For
deployments where screenshots must stay inside the network, ``local_only``
(``STATELOCK_PERCEPTION_LOCAL_ONLY``) refuses any model server that is not on this
machine or a private network, or a hosted provider (``check_local_endpoint``), at startup.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import socket
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from statelock.core.state import ActionContext, BrowserState

logger = logging.getLogger(__name__)

ExtractType = Literal["string", "number", "integer", "boolean"]
DEFAULT_TIMEOUT = 20.0  # seconds per model call
DEFAULT_RETRIES = 1  # more attempts after invalid output or an error
_PYTHON_TYPES: dict[str, type] = {"string": str, "number": float, "integer": int, "boolean": bool}
CACHE_SIZE = 256


class PerceptionRequest(BaseModel):
    """Input package for a visual policy check."""

    check_name: str
    instruction: str
    phase: Literal["pre", "post"] = "pre"
    # The action being checked (None for a standalone check, e.g. `statelock perception-check`).
    context: ActionContext | None = None
    # The page to judge: the pre-action state (pre-conditions) or post-action state (post-conditions).
    state: BrowserState | None = None
    # Values the model must read off the screenshot: {name: type}.
    extract: dict[str, ExtractType] = Field(default_factory=dict)

    def screenshot(self) -> str | None:
        state = self.state if self.state is not None else (self.context.browser_state if self.context else None)
        return state.screenshot_base64 if state else None


class PerceptionVerdict(BaseModel):
    """Schema-validated result from a visual evaluator."""

    passed: bool
    reason: str = ""
    extracted: dict[str, Any] = Field(default_factory=dict)
    # How the verdict was reached (model, attempts, latency, or why it failed closed).
    meta: dict[str, Any] = Field(default_factory=dict)


class PerceptionEvaluator(Protocol):
    async def evaluate(self, request: PerceptionRequest) -> PerceptionVerdict:
        """Evaluate a visual policy check against captured browser state."""
        ...


def fail_closed(reason: str, **meta: Any) -> PerceptionVerdict:
    return PerceptionVerdict(passed=False, reason=reason, meta={"failed_closed": True, **meta})


class NotConfiguredPerceptionEvaluator:
    """Default evaluator: fails closed, so visual rules block until a VLM is configured."""

    async def evaluate(self, request: PerceptionRequest) -> PerceptionVerdict:
        return fail_closed(
            "No perception evaluator is configured (STATELOCK_PERCEPTION_MODEL).", check_name=request.check_name
        )


def response_schema(extract: Mapping[str, str]) -> dict[str, Any]:
    """The strict JSON schema the model's answer must match."""
    return {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "reason": {"type": "string"},
            "extracted": {
                "type": "object",
                "properties": {name: {"type": [kind, "null"]} for name, kind in extract.items()},
                "required": sorted(extract),
                "additionalProperties": False,
            },
        },
        "required": ["passed", "reason", "extracted"],
        "additionalProperties": False,
    }


def _output_model(extract: Mapping[str, str]) -> type[BaseModel]:
    return _cached_output_model(tuple(sorted(extract.items())))


@lru_cache(maxsize=128)
def _cached_output_model(extract: tuple[tuple[str, str], ...]) -> type[BaseModel]:
    fields: dict[str, Any] = {name: (_PYTHON_TYPES[kind] | None, ...) for name, kind in extract}
    extracted = create_model("Extracted", __config__=ConfigDict(extra="forbid", strict=True), **fields)
    return create_model(
        "PerceptionOutput",
        __config__=ConfigDict(extra="forbid", strict=True),
        passed=(bool, ...),
        reason=(str, ...),
        extracted=(extracted, ...),
    )


SYSTEM_PROMPT = (
    "You check a screenshot of a web page for a browser-automation safety system. "
    "Answer only from what is visible in the screenshot. If the screenshot does not show "
    "enough to answer, set passed to false and say why. Reply with JSON only, matching "
    "the given schema: passed (true only if the check holds), reason (one sentence), "
    "extracted (each requested value exactly as shown on the page, or null if not visible)."
)


def build_messages(request: PerceptionRequest, image_base64: str) -> list[dict[str, Any]]:
    wanted = ", ".join(f"{name} ({kind})" for name, kind in request.extract.items()) or "none"
    text = (
        f"Check: {request.instruction}\n"
        f"Values to read from the page: {wanted}\n"
        f"JSON schema: {json.dumps(response_schema(request.extract), separators=(',', ':'))}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
            ],
        },
    ]


def parse_output(content: str, extract: Mapping[str, str]) -> PerceptionVerdict:
    """Validate the model's text against the strict schema. Raises ValueError."""
    text = content.strip()
    if text.startswith("```"):  # some models fence their JSON despite the instructions
        text = text.strip("`").removeprefix("json").strip()
    try:
        output = _output_model(extract).model_validate_json(text)
    except ValidationError as error:
        raise ValueError(f"output does not match the schema: {error.errors()[:3]}") from error
    data = output.model_dump()
    return PerceptionVerdict(passed=data["passed"], reason=data["reason"], extracted=data["extracted"])


Resolver = Callable[[str, None], list[tuple[Any, ...]]]


def _resolve(host: str, port: None) -> list[tuple[Any, ...]]:
    return socket.getaddrinfo(host, port)


# litellm providers for self-hosted servers, which send every request to ``api_base``.
# (A hosted provider's prefix can route to its own cloud whatever api_base says.)
LOCAL_PROVIDERS = frozenset({"ollama", "ollama_chat", "hosted_vllm", "openai", "lm_studio"})


def check_local_endpoint(model: str, api_base: str | None, *, resolve: Resolver = _resolve) -> None:
    """Raise ValueError unless the model is served by a self-hosted server on this machine or a
    private network. The model needs a self-hosted provider prefix (``LOCAL_PROVIDERS``), and
    ``api_base`` must be local (``LOCAL_NETWORKS``): a host name must resolve, and every
    address it resolves to must be local. Checked once, at startup."""
    provider = model.split("/", 1)[0] if "/" in model else ""
    if provider not in LOCAL_PROVIDERS:
        raise ValueError(
            f"STATELOCK_PERCEPTION_LOCAL_ONLY: {model!r} is not a self-hosted model; use one of the "
            f"providers {', '.join(sorted(LOCAL_PROVIDERS))} (e.g. ollama_chat/qwen2.5vl:7b)"
        )
    if not api_base:
        raise ValueError(
            "STATELOCK_PERCEPTION_LOCAL_ONLY is on, so the model server must be named: "
            "set STATELOCK_PERCEPTION_API_BASE to a server on this machine or your private network"
        )
    parts = urlsplit(api_base)
    host = parts.hostname
    if parts.scheme not in {"http", "https"} or not host:
        raise ValueError(f"STATELOCK_PERCEPTION_API_BASE is not an http(s) URL: {api_base!r}")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in resolve(host, None)}
        except (OSError, ValueError) as error:
            message = f"STATELOCK_PERCEPTION_LOCAL_ONLY: cannot resolve the model server {host!r}: {error}"
            raise ValueError(message) from None
    public = sorted(str(address) for address in addresses if not _is_local(address))
    if public or not addresses:
        raise ValueError(
            f"STATELOCK_PERCEPTION_LOCAL_ONLY: the model server {host!r} is not on this machine or a "
            f"private network (resolves to {', '.join(public) or 'nothing'})"
        )


# This machine and private networks only. An explicit list: Python's is_private also
# counts documentation and benchmark ranges, which are not anyone's private network.
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",  # loopback
        "10.0.0.0/8",  # RFC 1918
        "172.16.0.0/12",  # RFC 1918 (Docker networks)
        "192.168.0.0/16",  # RFC 1918
        "169.254.0.0/16",  # link-local
        "100.64.0.0/10",  # carrier-grade NAT, used by overlay networks such as Tailscale
        "::1/128",  # loopback
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
    )
)


def _is_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in LOCAL_NETWORKS)


class LiteLLMPerceptionEvaluator:
    """A vision-language model through litellm (local vLLM or Ollama, or a hosted model)."""

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        max_tokens: int = 512,
        local_only: bool = False,
        completion: Any = None,
    ) -> None:
        if timeout <= 0 or retries < 0:
            raise ValueError("the perception timeout must be > 0 and retries >= 0")
        if local_only:
            check_local_endpoint(model, api_base)
            # litellm otherwise fetches its model price list from the internet when imported.
            os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        self.local_only = local_only
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.max_tokens = max_tokens
        if completion is None:
            import litellm  # noqa: PLC0415 - optional dependency (statelock-ai[vlm])

            completion = litellm.acompletion
        self._completion = completion
        self._cache: OrderedDict[str, PerceptionVerdict] = OrderedDict()

    async def evaluate(self, request: PerceptionRequest) -> PerceptionVerdict:
        image = request.screenshot()
        if not image:
            return fail_closed("No screenshot was captured for the visual check.", model=self.model)
        key = hashlib.sha256(
            json.dumps([request.instruction, request.extract, image], sort_keys=True).encode()
        ).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:  # the same page and question (a reviewed action, a press and its release)
            self._cache.move_to_end(key)
            return cached.model_copy(update={"meta": {**cached.meta, "cached": True}})
        verdict = await self._ask(request, image)
        if not verdict.meta.get("failed_closed"):
            self._cache[key] = verdict
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return verdict

    async def _ask(self, request: PerceptionRequest, image: str) -> PerceptionVerdict:
        messages = build_messages(request, image)
        schema = response_schema(request.extract)
        started = time.monotonic()
        problems: list[str] = []
        for attempt in range(1, self.retries + 2):
            try:
                response = await asyncio.wait_for(
                    self._completion(
                        model=self.model,
                        messages=messages,
                        api_base=self.api_base,
                        api_key=self.api_key,
                        temperature=0,
                        max_tokens=self.max_tokens,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {"name": "visual_check", "schema": schema, "strict": True},
                        },
                        timeout=self.timeout,
                    ),
                    timeout=self.timeout,
                )
                content = response.choices[0].message.content or ""
                verdict = parse_output(content, request.extract)
            except (asyncio.TimeoutError, TimeoutError):
                problems.append(f"attempt {attempt}: timed out after {self.timeout} s")
            except ValueError as error:
                problems.append(f"attempt {attempt}: {error}")
            except Exception as error:  # noqa: BLE001 - any model or transport error fails closed
                problems.append(f"attempt {attempt}: {type(error).__name__}: {error}")
            else:
                verdict.meta = {
                    "model": self.model,
                    "attempts": attempt,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
                return verdict
        logger.warning("Visual check %s failed closed: %s", request.check_name, "; ".join(problems))
        return fail_closed(
            f"The visual check could not be evaluated: {problems[-1]}",
            model=self.model,
            attempts=len(problems),
            problems=problems,
        )
