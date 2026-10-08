from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(
        """
policies:
  - agent_id: finance_reconciliation_agent
    target_url_contains: /demo/finance
    pre_conditions:
      - assert_field_equal: {left: bank_deposit_amount, right: erp_invoice_amount}
    post_conditions:
      - trigger: {click_text: [Mark as Paid]}
        require_page_text: {values: [Reconciliation complete]}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """A Statelock proxy with the test site, for browser tests (see browser_support.py)."""
    browser_support = pytest.importorskip("browser_support")
    if not browser_support.chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    with browser_support.running_server(tmp_path_factory) as running:
        yield running
