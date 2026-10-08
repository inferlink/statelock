# SPDX-License-Identifier: Apache-2.0
"""Client SDK for Playwright agents that connect through Statelock.

    import statelock.client
    from statelock.client import StatelockPolicyViolationError

    statelock.client.install()   # Playwright's own uploads, downloads and page.request go through Statelock
    # STATELOCK_URL and STATELOCK_API_KEY (or server_url=..., api_key=...).
    async with await statelock.client.connect_playwright(playwright) as governed:
        try:
            async with governed.guard():
                await governed.page.goto("http://localhost:8010/demo/finance?scenario=mismatch")
                await governed.page.click("text=Mark as Paid")
        except StatelockPolicyViolationError as violation:
            print(violation.rule, violation.reason)

After ``install()``, Playwright's own file APIs work on the governed page:
``set_input_files`` and ``FileChooser.set_files`` upload through Statelock (as
governed uploads), and ``page.expect_download()`` returns the download once
Statelock has checked it.

The async API is the default. The sync API has the same names ending in ``_sync``
(functions) or ``Sync`` (classes): ``install_sync``, ``connect_playwright_sync``,
``PlaywrightSessionSync``, ``statelock_guard_sync``, ``StatelockDownloadSync``, ...
"""

from __future__ import annotations

from statelock.client.connection import StatelockConnection, connect_statelock
from statelock.client.files import DownloadWaiter, StatelockDownload, set_input_files
from statelock.client.install import install, statelock_session, uninstall
from statelock.client.playwright import (
    PlaywrightSession,
    PlaywrightSessionSync,
    connect_playwright,
    connect_playwright_sync,
)
from statelock.client.requests import StatelockRequestError, StatelockResponse, statelock_fetch
from statelock.client.sessions import (
    SessionUrl,
    SessionUrlError,
    create_session_url,
    delete_saved_session,
    list_saved_sessions,
)
from statelock.client.sync import (
    DownloadWaiterSync,
    StatelockDownloadSync,
    install_sync,
    set_input_files_sync,
    statelock_session_sync,
    uninstall_sync,
)
from statelock.client.transfer import StatelockDownloadError, UploadFile
from statelock.client.violations import StatelockPolicyViolationError, statelock_guard, statelock_guard_sync

__all__ = [
    "DownloadWaiter",
    "DownloadWaiterSync",
    "PlaywrightSession",
    "PlaywrightSessionSync",
    "SessionUrl",
    "SessionUrlError",
    "StatelockConnection",
    "StatelockDownload",
    "StatelockDownloadError",
    "StatelockDownloadSync",
    "StatelockPolicyViolationError",
    "StatelockRequestError",
    "StatelockResponse",
    "UploadFile",
    "connect_playwright",
    "connect_playwright_sync",
    "connect_statelock",
    "create_session_url",
    "delete_saved_session",
    "install",
    "install_sync",
    "list_saved_sessions",
    "set_input_files",
    "set_input_files_sync",
    "statelock_fetch",
    "statelock_guard",
    "statelock_guard_sync",
    "statelock_session",
    "statelock_session_sync",
    "uninstall",
    "uninstall_sync",
]
