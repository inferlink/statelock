# SPDX-License-Identifier: Apache-2.0
"""Which of the browser's cookies an agent may read (policy ``cookie_access``).

By default an agent reads none: with the site's session cookies, agent-side HTTP
(Playwright ``page.request``, cookies copied into another client) would act as the
logged-in user outside Statelock. A policy can allow specific cookies:

    cookie_access:
      names: [csrftoken, consent]      # exact cookie names
      domains: [cdn.example.com]       # cookies of these domains (and subdomains)
      # all: true                      # every cookie: knowingly allows the bypass

Access is per agent: the union of the agent's policies. Statelock returns only the
allowed cookies and records their names (never their values).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CookieAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    all: bool = False

    @model_validator(mode="after")
    def _something_allowed(self) -> CookieAccess:
        if not (self.all or self.names or self.domains):
            raise ValueError("cookie_access needs names, domains or all: true")
        self.domains = [domain.lower().lstrip(".") for domain in self.domains]
        return self

    @classmethod
    def merge(cls, accesses: list[CookieAccess]) -> CookieAccess | None:
        if not accesses:
            return None
        return cls(
            names=sorted({name for access in accesses for name in access.names}),
            domains=sorted({domain for access in accesses for domain in access.domains}),
            all=any(access.all for access in accesses),
        )

    def allows(self, cookie: dict[str, Any]) -> bool:
        if self.all or cookie.get("name") in self.names:
            return True
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        return any(domain == allowed or domain.endswith("." + allowed) for allowed in self.domains)

    def filter(self, cookies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(allowed, withheld)."""
        allowed = [cookie for cookie in cookies if self.allows(cookie)]
        withheld = [cookie for cookie in cookies if not self.allows(cookie)]
        return allowed, withheld
