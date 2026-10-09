"""Factories for standard holdings in tests (no PII, no 9+ digit runs)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from nivesh_core.holdings import Holding, Txn

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2026, 1, 5)
ISIN_A, ISIN_B, ISIN_C = "INE000A01010", "INE111A01011", "INE222B01012"


def holding(**kw: Any) -> Holding:
    base: dict[str, Any] = {
        "isin": ISIN_A, "symbol": "RELI", "exchange": "NSE", "name": "Reliance",
        "asset_class": "equity", "quantity": D(10), "avg_cost": D(100), "price": D(120),
        "price_basis": "previous_close", "as_of": DAY, "source": "investright",
        "source_label": "InvestRight", "holder_ref": "", "unresolved": False,
    }  # fmt: skip
    base.update(kw)
    if "value_inr" not in base:
        base["value_inr"] = base["quantity"] * base["price"]
    return Holding(**base)


def txn(**kw: Any) -> Txn:
    base: dict[str, Any] = {
        "isin": ISIN_B, "symbol": ISIN_B, "exchange": "AMFI", "name": "Fund B", "asset_class": "mf",
        "holder_ref": "abcdefghijkl", "txn_date": DAY, "txn_type": "purchase",
        "quantity": D("10.5"), "price": D("20"), "amount": D("210"),
    }  # fmt: skip
    base.update(kw)
    return Txn(**base)


def fixture_http(
    status: int = 200, only: str | None = None
) -> tuple[httpx.Client, list[httpx.Request]]:
    """An httpx client serving tests/fixtures/investright/*.json by API path; records requests."""
    seen: list[httpx.Request] = []
    by_path = {
        "/oapi/v1/portfolio/holdings": "holdings",
        "/oapi/v1/cumulative-positions": "positions",
        "/oapi/v1/user/margins": "margins",
        "/oapi/v1/fetch-ltp": "ltp",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        name = only or by_path[req.url.path]
        return httpx.Response(status, text=(FIXTURES / "investright" / f"{name}.json").read_text())

    return httpx.Client(transport=httpx.MockTransport(handler)), seen
