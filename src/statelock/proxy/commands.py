# SPDX-License-Identifier: Apache-2.0
"""CDP commands Statelock refuses outright."""

from __future__ import annotations

import re
from typing import Any

from statelock.core.enums import SystemRule
from statelock.core.jsonutil import as_dict

AGENT_NAVIGATION_METHODS = {"Page.navigate", "Page.reload", "Page.navigateToHistoryEntry"}
# Network interception by the agent (Playwright page.route, route.fulfill, context
# HTTP credentials) can fake or change what the page loads and sends, so the
# evidence and the pre-conditions would describe a page the site never served.
NETWORK_INTERCEPTION_METHODS = {"Network.setRequestInterception", "Network.setBlockedURLs"}
NETWORK_INTERCEPTION_DOMAIN = "Fetch."
# Commands that act outside governance: page code with its own CDP channel, a site
# request sent again with the user's cookies, a document written by the agent.
UNGOVERNED_METHODS = {
    "Target.exposeDevToolsProtocol": "it gives page code its own CDP connection, outside Statelock",
    "Network.replayXHR": "it sends a site request again, with the user's cookies, without real input",
    "Page.setDocumentContent": "it replaces the page with the agent's HTML, whose scripts would run untagged",
}
# Commands that load a URL in the browser, which runs on the Statelock host. Only
# web pages: file:, chrome:, data:, blob: and the like could read the host's files
# or run agent-written documents as the site.
URL_METHODS = {"Page.navigate", "Target.createTarget", "Network.loadNetworkResource"}
WEB_SCHEMES = ("http://", "https://")
BLANK_URL = "about:blank"
# The URL parser drops tabs and newlines anywhere and C0 controls and spaces at either end.
_IGNORED_URL_CHARACTERS = re.compile(r"[\t\n\r]")


def normalized_url(url: Any) -> str:
    return _IGNORED_URL_CHARACTERS.sub("", str(url or "")).strip("\x00- ").lower()


def _url_refusal(method: str, url: str) -> tuple[str, str] | None:
    if url.startswith(WEB_SCHEMES) or (url == BLANK_URL and method != "Network.loadNetworkResource"):
        return None
    if url.startswith("javascript:"):
        return (
            SystemRule.JAVASCRIPT_URL.value,
            f"Blocked {method} to a javascript: URL: it runs untagged code in the page. "
            "Use page.goto() with an http(s) URL, or real input.",
        )
    allowed = "an http(s) URL" if method == "Network.loadNetworkResource" else "an http(s) URL or about:blank"
    return (
        SystemRule.NAVIGATION_URL.value,
        f"Blocked {method} to {url[:80]!r}: Statelock loads only {allowed}. The browser runs on the "
        "Statelock host, so other schemes (file:, data:, blob:, chrome:, ...) are refused.",
    )


def _network_configuration_refusal(method: Any, params: dict[str, Any]) -> tuple[str, str] | None:
    if isinstance(method, str) and (
        method.startswith(NETWORK_INTERCEPTION_DOMAIN) or method in NETWORK_INTERCEPTION_METHODS
    ):
        return (
            SystemRule.AGENT_NETWORK_INTERCEPTION.value,
            f"Blocked {method}: agent network interception (page.route, route.fulfill, "
            "HTTP credentials) could change or fake what the site sends and receives. "
            "Remove the routes and let the page load normally.",
        )
    if method == "Target.createBrowserContext" and any(params.get(key) for key in ("proxyServer", "proxyBypassList")):
        return SystemRule.AGENT_NETWORK_INTERCEPTION.value, "Blocked agent-selected browser proxy."
    if method == "Security.setIgnoreCertificateErrors" and params.get("ignore") is True:
        return SystemRule.AGENT_NETWORK_INTERCEPTION.value, "Blocked ignoring certificate errors."
    if (method == "Security.setOverrideCertificateErrors" and params.get("override") is True) or (
        method == "Security.handleCertificateError"
    ):
        # The agent would decide which invalid certificates the browser accepts.
        return (
            SystemRule.AGENT_NETWORK_INTERCEPTION.value,
            f"Blocked {method}: certificate errors cannot be overridden.",
        )
    return None


def refused_command(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Returns (rule, reason) for a refused command, or None."""
    method = payload.get("method")
    params = as_dict(payload.get("params"))
    if method == "Target.sendMessageToTarget":
        return (
            SystemRule.WRAPPED_COMMAND.value,
            "Blocked Target.sendMessageToTarget: wrapped CDP commands are not supported by "
            "Statelock. Send commands on flattened sessions (sessionId on each message), as "
            "Playwright and Puppeteer do.",
        )
    if method == "Debugger.setScriptSource":
        return (
            SystemRule.SCRIPT_MODIFICATION.value,
            "Blocked Debugger.setScriptSource: changing the site's scripts would make agent code look like site code.",
        )
    if isinstance(method, str) and method in UNGOVERNED_METHODS:
        return SystemRule.UNGOVERNED_COMMAND.value, f"Blocked {method}: {UNGOVERNED_METHODS[method]}."
    if network_refusal := _network_configuration_refusal(method, params):
        return network_refusal
    if method in URL_METHODS:
        # Page.navigate with a frameId (an iframe) is checked the same way.
        return _url_refusal(str(method), normalized_url(params.get("url")))
    return None
