# SPDX-License-Identifier: Apache-2.0
"""Demo pages, served only when STATELOCK_DEMO=1."""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

router = APIRouter()

MATCH_AMOUNT = "$5,000.00"
MISMATCH_AMOUNT = "$4,900.00"


@router.get("/demo/finance", response_class=HTMLResponse)
async def finance_demo(scenario: str = Query(default="mismatch")) -> str:
    """Bank deposit vs ERP invoice. scenario=mismatch shows different amounts."""
    bank_amount = MISMATCH_AMOUNT if scenario == "mismatch" else MATCH_AMOUNT
    template = files(__package__).joinpath("finance.html").read_text(encoding="utf-8")
    return template.replace("__BANK_AMOUNT__", bank_amount).replace("__ERP_AMOUNT__", MATCH_AMOUNT)


# Two-page portal demo: the bank deposit is on one page and the ERP invoice on
# another, so the policy remembers the deposit and compares it on the ERP page.
# scenario: match (5,000 / 5,000), mismatch (4,900 / 5,000), large (25,000 / 25,000).
PORTAL_AMOUNTS = {
    "match": ("$5,000.00", "$5,000.00"),
    "mismatch": ("$4,900.00", "$5,000.00"),
    "large": ("$25,000.00", "$25,000.00"),
}


def _portal_amounts(scenario: str) -> tuple[str, str]:
    return PORTAL_AMOUNTS.get(scenario, PORTAL_AMOUNTS["match"])


@router.get("/demo/bank", response_class=HTMLResponse)
async def bank_demo(scenario: str = Query(default="match")) -> str:
    template = files(__package__).joinpath("bank.html").read_text(encoding="utf-8")
    return template.replace("__BANK_AMOUNT__", _portal_amounts(scenario)[0])


@router.get("/demo/erp", response_class=HTMLResponse)
async def erp_demo(scenario: str = Query(default="match")) -> str:
    template = files(__package__).joinpath("erp.html").read_text(encoding="utf-8")
    return template.replace("__ERP_AMOUNT__", _portal_amounts(scenario)[1])
