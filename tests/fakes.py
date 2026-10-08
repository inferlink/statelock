"""Test doubles for CDP connections."""

from __future__ import annotations

from typing import Any

from statelock.proxy.connection import CdpError


class FakeConnection:
    """Records sent commands; returns canned results by method."""

    def __init__(self, results: dict[str, Any] | None = None, fail: set[str] | None = None) -> None:
        self.results = results or {}
        self.fail = fail or set()
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.listeners: list[Any] = []
        self._attach_count = 0

    def add_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def emit(self, message: dict[str, Any]) -> None:
        for listener in self.listeners:
            listener(message)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,  # noqa: ARG002 - matches CdpConnection.send
    ) -> dict[str, Any]:
        self.sent.append((method, params or {}, session_id))
        if method in self.fail:
            raise CdpError(f"{method} failed")
        if method == "Target.attachToTarget":
            self._attach_count += 1
            return {"sessionId": f"G{self._attach_count}"}
        result = self.results.get(method, {})
        return result(params) if callable(result) else result

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.sent]
