# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory.

Run with: uvicorn --factory statelock.app:create_app
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket

from statelock import __version__
from statelock.auth import Identity
from statelock.demo import router as demo_router
from statelock.dependencies import authenticated_auditor, authenticated_identity
from statelock.plugins import PluginContext, discover_plugins, setup_plugins
from statelock.proxy.bridge import CdpBridge
from statelock.review.api import authenticated_reviewer
from statelock.review.api import router as review_router
from statelock.services import Services, get_services
from statelock.sessions import SESSIONS_PATH, install_log_redaction
from statelock.sessions import router as sessions_router
from statelock.settings import Settings
from statelock.wire import VIOLATION_ENDPOINT_PREFIX, WEBSOCKET_PATH

__all__ = ["authenticated_auditor", "authenticated_identity", "authenticated_reviewer", "create_app", "get_services"]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


core_router = APIRouter()


@core_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@core_router.get(VIOLATION_ENDPOINT_PREFIX + "/{session_id}")
async def get_violation(
    session_id: str, request: Request, identity: Annotated[Identity | None, Depends(authenticated_identity)]
) -> dict[str, Any]:
    violation = get_services(request.app).registry.get(session_id)
    # With authentication on, an agent sees only its own sessions (others look absent).
    if violation is None or (identity is not None and violation.get("agent_id") != identity.agent_id):
        raise HTTPException(status_code=404, detail="No violation recorded for session")
    return violation


@core_router.websocket(WEBSOCKET_PATH)
async def statelock_session(websocket: WebSocket) -> None:
    await CdpBridge(get_services(websocket.app)).handle(websocket)


@core_router.websocket(SESSIONS_PATH + "/{token}/devtools")
async def statelock_session_url(websocket: WebSocket, token: str) -> None:
    """A session opened from a session URL: the token identifies the agent (no headers)."""
    await CdpBridge(get_services(websocket.app)).handle(websocket, token)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    """Build services once, register routes, and load plugins."""
    settings = settings or (services.settings if services else Settings())
    if settings.log_level:
        # No-op when the host (e.g. a test runner) already configured logging.
        logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
    services = services or Services.from_settings(settings)

    app = FastAPI(title="Statelock", version=__version__)
    app.state.services = services
    app.include_router(core_router)
    app.include_router(review_router)
    app.include_router(sessions_router)
    install_log_redaction()
    if settings.demo:
        app.include_router(demo_router)
    setup_plugins(PluginContext(app=app, services=services), discover_plugins(settings.plugins))
    return app
