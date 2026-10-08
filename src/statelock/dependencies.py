# SPDX-License-Identifier: Apache-2.0
"""FastAPI dependencies shared by Statelock routes and plugins."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from statelock.auth import AUTHORIZATION_HEADER, Auditor, AuthError, Identity
from statelock.services import get_services

logger = logging.getLogger(__name__)


def authenticated_identity(request: Request) -> Identity | None:
    """FastAPI dependency: the calling agent (Bearer key), or None when authentication is off.

    Raises 401 when authentication is on and the key is missing or invalid.
    Plugins can use it for their own routes.
    """
    authenticator = get_services(request.app).authenticator
    if not authenticator.enabled:
        return None
    try:
        return authenticator.authenticate(None, request.headers.get(AUTHORIZATION_HEADER))
    except AuthError as error:
        logger.warning("Rejected %s: authentication failed: %s", request.url.path, error)
        raise HTTPException(
            status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"}
        ) from error


def authenticated_auditor(request: Request) -> Auditor:
    """FastAPI dependency for plugin routes that serve evidence: an auditor key (keys file
    ``auditors:``). With authentication off, a local auditor that sees every tenant. 401 otherwise."""
    try:
        return get_services(request.app).authenticator.authenticate_auditor(request.headers.get(AUTHORIZATION_HEADER))
    except AuthError as error:
        logger.warning("Rejected %s: auditor authentication failed: %s", request.url.path, error)
        raise HTTPException(
            status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"}
        ) from error
