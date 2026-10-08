# SPDX-License-Identifier: Apache-2.0
"""The agent's cookie reads (Storage.getCookies, Network.getCookies, Network.getAllCookies).

Statelock runs the read on its own CDP connection and answers the agent with only
the cookies its policies allow (policy ``cookie_access``); with no access, the
read is declined. Either way the read is recorded with cookie names, never values.
"""

from __future__ import annotations

from typing import Any

from statelock.core.enums import SystemRule
from statelock.core.jsonutil import as_dict, as_str
from statelock.core.verdict import PolicyVerdict
from statelock.policy.cookies import CookieAccess
from statelock.proxy.pages import PageSessions
from statelock.proxy.targets import TargetRegistry
from statelock.wire import statelock_error

COOKIE_METHODS = {"Storage.getCookies", "Network.getCookies", "Network.getAllCookies"}
NO_ACCESS_REASON = (
    "Statelock does not let this agent read the browser's cookies: HTTP requests made "
    "with them (Playwright page.request / context.request, cookies copied into another "
    "client) would act as the logged-in user without being governed or recorded. "
    "Load pages and files through the browser, make HTTP requests through Statelock "
    "(policy request_access; page.request after statelock.client.install()), or allow "
    "specific cookies with cookie_access in the agent's policy."
)


class CookieReads:
    def __init__(self, pages: PageSessions, targets: TargetRegistry, access: CookieAccess | None) -> None:
        self.pages = pages
        self.targets = targets
        self.access = access

    async def answer(self, payload: dict[str, Any]) -> tuple[dict[str, Any], PolicyVerdict]:
        """(the CDP response body for the agent, the verdict to record)."""
        method = str(payload.get("method"))
        evidence: dict[str, Any] = {"method": method}
        if self.access is None:
            verdict = PolicyVerdict.block(
                reason=NO_ACCESS_REASON, rule=SystemRule.COOKIE_EXPORT.value, evidence=evidence
            )
            return statelock_error(NO_ACCESS_REASON), verdict
        cookies = await self._read(method, as_dict(payload.get("params")), as_str(payload.get("sessionId")))
        allowed, withheld = self.access.filter(cookies)
        evidence.update(
            cookies_returned=sorted({str(c.get("name")) for c in allowed}),
            cookies_withheld=len(withheld),
        )
        verdict = PolicyVerdict.allow(
            f"Returned {len(allowed)} cookie(s) allowed by cookie_access; withheld {len(withheld)}.", evidence
        )
        return {"result": {"cookies": allowed}}, verdict

    async def _read(self, method: str, params: dict[str, Any], agent_session_id: str | None) -> list[dict[str, Any]]:
        target_id = self.targets.target_for(agent_session_id)
        connection = self.pages.connection
        if method.startswith("Network.") and target_id is not None:
            session_id = await self.pages.session_for(target_id)
            result = await connection.send(method, params, session_id=session_id)
        else:
            # Browser-level read (Playwright context.cookies()): the context's cookie store.
            store = {"browserContextId": params["browserContextId"]} if params.get("browserContextId") else {}
            result = await connection.send("Storage.getCookies", store)
        return [c for c in result.get("cookies", []) if isinstance(c, dict)]
