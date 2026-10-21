# SPDX-License-Identifier: Apache-2.0
"""Local Policy Studio UI and LiteLLM bridge.

The API accepts only loopback Host names, same-origin requests, and the random token
embedded in the page this process serves. It listens on 127.0.0.1 unless
STATELOCK_POLICY_STUDIO_HOST says otherwise; the compose service listens on 0.0.0.0
inside a network of its own, published only on the host's loopback.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import importlib
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Any

from draft_with_model import (
    DEFAULT_MODEL,
    LIMITS,
    MAX_BODY_BYTES,
    MODEL_KEY_ENV,
    check_context,
    check_turns,
    ensure_model_key,
    model_messages,
    parse_response,
    system_prompt,
    validate_yaml,
)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from statelock.policy.extensions import load_rule_modules

ROOT = Path(__file__).resolve().parent
# Empty values (compose passes unset variables through as "") mean the default.
MODEL = os.environ.get("STATELOCK_POLICY_STUDIO_MODEL") or DEFAULT_MODEL
API_BASE = os.environ.get("STATELOCK_POLICY_STUDIO_API_BASE") or None
RULE_MODULES = os.environ.get("STATELOCK_RULE_MODULES", "")
# Per process: only a page served by this process can call the API.
TOKEN = secrets.token_urlsafe(32)
TOKEN_HEADER = "X-Statelock-Studio-Token"  # noqa: S105 - a header name
LOGGER = logging.getLogger("uvicorn.error")
PROVIDER_ERROR_CODES = frozenset(
    {
        "API_KEY_INVALID",
        "API_KEY_EXPIRED",
        "API_KEY_SERVICE_BLOCKED",
        "API_KEY_HTTP_REFERRER_BLOCKED",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "SERVICE_DISABLED",
        "BILLING_DISABLED",
        "BILLING_NOT_ACTIVE",
        "INVALID_ARGUMENT",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "FAILED_PRECONDITION",
        "RESOURCE_EXHAUSTED",
        "NOT_FOUND",
    }
)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail startup rather than report a missing or broken rule module as a schema error later.
    load_rule_modules(RULE_MODULES)
    # The model's rule list comes from the registry, so it includes the custom rules just loaded.
    app.state.system_prompt = system_prompt()
    # LiteLLM takes seconds to import: do it before serving, not on the event loop mid-request.
    app.state.completion = importlib.import_module("litellm").completion
    try:
        version = metadata.version("litellm")
    except metadata.PackageNotFoundError:
        version = "not installed"
    key = os.environ.get(MODEL_KEY_ENV, "")
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:12] if key else "none"
    LOGGER.info(
        "Policy Studio startup: model=%r api_base=%r litellm=%s key_variable=%s key_present=%s key_fingerprint=%s",
        MODEL,
        API_BASE,
        version,
        MODEL_KEY_ENV,
        bool(key),
        fingerprint,
    )
    try:
        ensure_model_key(MODEL)
    except ValueError as error:
        LOGGER.warning("Policy Studio: %s", error)
    yield


def safe_model_error(error: Exception) -> str:
    parts = [type(error).__name__]
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        status = getattr(error, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        parts.append(f"HTTP {status}")
    # Provider errors may echo secrets or prompts. Emit only known machine codes,
    # regardless of whether the embedded error body is an object, list, or text.
    body = str(error)
    response_text = getattr(response, "text", "")
    if isinstance(response_text, str):
        body += "\n" + response_text
    parts.extend(code for code in sorted(PROVIDER_ERROR_CODES) if re.search(rf"\b{code}\b", body))
    return "; ".join(parts)


app = FastAPI(title="Statelock Policy Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


async def _read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length and (not length.isdecimal() or int(length) > MAX_BODY_BYTES):
        raise HTTPException(413, "Request too large")
    # Chunked bodies carry no Content-Length: stop reading at the cap.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "Request too large")
    return bytes(body)


async def _request_data(request: Request) -> tuple[dict[str, Any], list[dict[str, Any]], str, bool]:
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Policy Studio only accepts same-origin requests")
    if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, "").encode(), TOKEN.encode()):
        raise HTTPException(403, "Reload Policy Studio: this page is not from the running server")
    raw = await _read_body(request)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"context", "turns", "message", "draft_requested"}:
            raise HTTPException(400, "Invalid draft request")
        context = check_context(payload["context"])
        turns = check_turns(payload["turns"])
        message = payload["message"]
        draft_requested = payload["draft_requested"]
        model_messages(context, turns, message, prompt=request.app.state.system_prompt, draft_requested=draft_requested)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from None
    return context, turns, message, draft_requested


@app.post("/api/preview")
async def preview(request: Request) -> JSONResponse:
    context, turns, message, draft_requested = await _request_data(request)
    messages = model_messages(
        context, turns, message, prompt=request.app.state.system_prompt, draft_requested=draft_requested
    )
    return JSONResponse({"model": MODEL, "messages": messages})


@app.post("/api/draft")
async def draft(request: Request) -> JSONResponse:
    context, turns, message, draft_requested = await _request_data(request)
    try:
        api_key = ensure_model_key(MODEL)
    except ValueError as error:
        raise HTTPException(503, str(error)) from None
    options = {"api_base": API_BASE} if API_BASE else {}
    try:
        messages = model_messages(
            context, turns, message, prompt=request.app.state.system_prompt, draft_requested=draft_requested
        )
        response = await asyncio.to_thread(
            request.app.state.completion,
            model=MODEL,
            messages=messages,
            api_key=api_key,
            timeout=90,
            response_format={"type": "json_object"},
            **options,
        )
        content = response.choices[0].message.content
    except Exception as error:  # noqa: BLE001 - provider exceptions may contain credentials or prompt text
        detail = safe_model_error(error)
        LOGGER.warning("Policy Studio model request failed: model=%r %s", MODEL, detail)
        raise HTTPException(502, f"Model request failed ({detail}); check key and provider access") from None
    try:
        block_runs = [run["run"] for run in context["runs"] if run["kind"] == "block"]
        assistant, result = parse_response(content, len(context["notes"]), block_runs, draft_requested=draft_requested)
    except (TypeError, ValueError) as error:
        raise HTTPException(502, str(error)) from None
    validation_error = None
    if result["yaml"] is not None:
        try:
            validate_yaml(result["yaml"], context["agent_id"])
        except (TypeError, ValueError) as error:
            validation_error = str(error)
            result["yaml"] = None
    return JSONResponse({"result": result, "assistant": assistant, "validation_error": validation_error})


@app.get("/")
async def index() -> HTMLResponse:
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    page = page.replace("{{token}}", TOKEN).replace("{{limits}}", html.escape(json.dumps(LIMITS)))
    return HTMLResponse(page)


@app.get("/{name}")
async def asset(name: str) -> FileResponse:
    if name not in {"studio.js", "core.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(ROOT / name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("STATELOCK_POLICY_STUDIO_HOST", "127.0.0.1"), port=8765)
