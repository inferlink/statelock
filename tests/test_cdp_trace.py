"""The statelock.cdp trace: one line per CDP message, ids only, never page data."""

from __future__ import annotations

from statelock.proxy.connection import describe


def test_trace_line_has_ids_but_no_page_data() -> None:
    line = describe(
        {
            "id": 7,
            "method": "Runtime.evaluate",
            "sessionId": "ABC123",
            "params": {"expression": "document.cookie", "frameId": "F1", "url": "https://x.test/?token=secret"},
        }
    )
    assert "id=7" in line and "Runtime.evaluate" in line and "session=ABC123" in line and "frameId=F1" in line
    assert "cookie" not in line and "secret" not in line and "token" not in line


def test_agent_controlled_values_are_quoted_and_cut_short() -> None:
    line = describe({"method": "Runtime.addBinding", "params": {"name": "fake\nINFO forged line" + "x" * 500}})
    assert "\n" not in line
    assert "'fake\\nINFO forged line" in line
    assert len(line) < 200
