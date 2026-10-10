"""Synthetic builders for US holdings, lots and USDINR series (no PII, no 9+ digit runs)."""

from datetime import date
from decimal import Decimal
from typing import Any

from nivesh_core.holdings import Holding, Lot

D = Decimal
DAY = date(2026, 1, 5)


def usd_holding(**kw: Any) -> Holding:
    base: dict[str, Any] = {
        "isin": None, "symbol": "AAPL", "exchange": "NASDAQ", "name": "Apple Inc",
        "asset_class": "equity", "quantity": D(10), "avg_cost": D(100), "price": D(100),
        "price_basis": "avg_cost", "value_inr": None, "as_of": DAY, "source": "us_csv",
        "source_label": "US brokerage", "holder_ref": "", "currency": "USD",
    }  # fmt: skip
    base.update(kw)
    return Holding(**base)


def lot(**kw: Any) -> Lot:
    base: dict[str, Any] = {
        "symbol": "AAPL", "exchange": "NASDAQ", "currency": "USD", "holder_ref": "",
        "acquired_on": date(2025, 1, 2), "quantity": D(4), "cost_per_unit": D(90),
        "source": "us_csv", "source_label": "US brokerage",
    }  # fmt: skip
    base.update(kw)
    return Lot(**base)


def usdinr_obs(*pairs: tuple[str, str]) -> list[tuple[date, Decimal]]:
    return [(date.fromisoformat(d), D(v)) for d, v in pairs]


class FakeBundledBroker:
    """A broker MCP that bundles trade tools with read tools (tool names are data only)."""

    TOOLS = (
        "place_order", "cancel_order", "close_position", "get_positions", "get_account_info",
    )  # fmt: skip

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": n, "description": f"{n} tool"} for n in self.TOOLS]

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, args))
        return {"tool": name, "ok": True}
