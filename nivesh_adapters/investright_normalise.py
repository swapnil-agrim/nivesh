"""Map InvestRight rows to the standard holding schema (ST-2.4). Row field names are per spec,
unverified against live API."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_core.holdings import Holding, PriceBasis
from nivesh_core.security_resolver import SecurityResolver

NAME = "investright"
__all__ = ["ltp_map", "normalise_holdings", "parse_decimal"]


def ltp_map(rows: Sequence[dict[str, Any]]) -> dict[str, Decimal]:
    """security_id -> last traded price from an LTP response."""
    return {
        str(r["security_id"]): parse_decimal(NAME, "ltp", r["ltp"])
        for r in rows
        if r.get("security_id") and r.get("ltp") not in (None, "")
    }


def normalise_holdings(
    rows: Sequence[dict[str, Any]],
    resolver: SecurityResolver,
    ltp: dict[str, Decimal],
    as_of: date,
) -> list[Holding]:
    out: list[Holding] = []
    for i, row in enumerate(rows):
        where = f"rows[{i}]"
        isin = str(row.get("isin") or "").strip() or None
        sec_id = str(row.get("security_id") or "").strip()
        if not isin and not sec_id:
            raise DataQualityError(NAME, where, None, "row needs an isin or a security_id")
        qty = parse_decimal(NAME, f"{where}.quantity", row.get("quantity"))
        if qty == 0:
            continue
        found = resolver.resolve(isin) if isin else None
        symbol = sec_id or (found.symbol if found else str(isin))
        name = str(row.get("name") or "").strip() or (found.name if found else None)
        exchange = str(row.get("exchange") or "").strip() or (found.exchange if found else "NSE")
        if not sec_id and found is None:
            exchange = "ISIN"  # unresolved placeholder: symbol is the ISIN (fits UNIQUE)
        basis: PriceBasis = "ltp" if symbol in ltp else "previous_close"
        if symbol in ltp:
            price = ltp[symbol]
        elif row.get("close_price") not in (None, ""):
            price = parse_decimal(NAME, f"{where}.close_price", row["close_price"])
        else:
            raise DataQualityError(NAME, f"{where}.close_price", None, "row has no usable price")
        avg = row.get("average_price")
        avg_cost = (
            parse_decimal(NAME, f"{where}.average_price", avg) if avg not in (None, "") else None
        )
        out.append(
            Holding(
                isin=isin, symbol=symbol, exchange=exchange, name=name,
                asset_class=found.asset_class if found else "equity",
                quantity=qty, avg_cost=avg_cost, price=price, price_basis=basis,
                value_inr=qty * price, as_of=as_of, source="investright",
                source_label="InvestRight", holder_ref="",
                amfi_code=found.amfi_code if found else None,
                unresolved=not sec_id and found is None,
            )
        )  # fmt: skip
    return out
