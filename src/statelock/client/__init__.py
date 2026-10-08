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
Statelock has checked it. Without ``install()``, the session object has the same
two methods: ``governed.set_input_files(target, files)`` and
``governed.expect_download()``.

The async API is the default. Most of it has a sync version with the same name
ending in ``_sync`` (functions) or ``Sync`` (classes): ``install_sync``,
``connect_playwright_sync``, ``PlaywrightSessionSync``, ``statelock_guard_sync``,
``expect_download_sync``, ``StatelockDownloadSync``, ... ``statelock_fetch`` is async
only; ``create_session_url`` and the saved-session functions are plain blocking
calls that both APIs use.
"""

from __future__ import annotations

from statelock.client.files import (
    DownloadWaiter,
    StatelockDownload,
    expect_download,
    set_input_files,
    statelock_session,
)
from statelock.client.files_sync import (
    DownloadWaiterSync,
    StatelockDownloadSync,
    expect_download_sync,
    set_input_files_sync,
    statelock_session_sync,
)
from statelock.client.install import install, uninstall
from statelock.client.install_sync import install_sync, uninstall_sync
from statelock.client.playwright import (
    PlaywrightSession,
    PlaywrightSessionSync,
    connect_playwright,
    connect_playwright_sync,
)
from statelock.client.requests import StatelockRequestError, StatelockResponse, statelock_fetch
from statelock.client.sessions import (
    SessionUrl,
    StatelockClientError,
    create_session_url,
    delete_saved_session,
    list_saved_sessions,
)
from statelock.client.transfer import StatelockDownloadError, UploadFile
from statelock.client.violations import StatelockPolicyViolationError, statelock_guard, statelock_guard_sync

__all__ = [
    "DownloadWaiter",
    "DownloadWaiterSync",
    "PlaywrightSession",
    "PlaywrightSessionSync",
    "SessionUrl",
    "StatelockClientError",
    "StatelockDownload",
    "StatelockDownloadError",
    "StatelockDownloadSync",
    "StatelockPolicyViolationError",
    "StatelockRequestError",
    "StatelockResponse",
    "UploadFile",
    "connect_playwright",
    "connect_playwright_sync",
    "create_session_url",
    "delete_saved_session",
    "expect_download",
    "expect_download_sync",
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
