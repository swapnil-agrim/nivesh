"""US holdings CSV import (ST-3.1): generic template plus Alpaca and Robinhood-export presets.

Columns follow `templates/holdings_us.csv` (a documentation copy; this module never reads it).
A CSV has no market price, so price is the average cost (`price_basis` `avg_cost`, book value) and
INR is derived later from the stored USDINR series, never at import. Preset header names are as
documented, unverified against a real export (deferred D3). The India importer `csv_import.py` is
a separate module and still rejects foreign currency.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from nivesh_adapters.csv_import import CsvError, PresetError
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_core.holdings import Holding, Lot
from nivesh_core.security_resolver import Resolved, normalise_us_exchange

COLUMNS = (
    "account_label",
    "symbol",
    "exchange",
    "quantity",
    "avg_cost",
    "currency",
    "asset_class",
    "acquired_on",
)
ASSET_CLASSES = ("equity", "etf")
VENUES = "NYSE, NASDAQ, ARCA"
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
NAME = "us_csv"

# Template column -> candidate headers in the export (case-insensitive, first match wins).
PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "alpaca": {
        "symbol": ("symbol",),
        "exchange": ("exchange",),
        "quantity": ("qty", "quantity"),
        "avg_cost": ("avg_entry_price", "average entry price"),
        "acquired_on": ("acquired_on", "date acquired"),
    },
    "robinhood": {
        "symbol": ("symbol", "instrument"),
        "quantity": ("quantity", "shares"),
        "avg_cost": ("average cost", "average buy price", "avg cost"),
        "acquired_on": ("date acquired", "acquired date", "acquired"),
    },
}


class UsResolver(Protocol):
    def resolve_symbol(self, symbol: str, exchange_hint: str = "") -> Resolved | None: ...


@dataclass(frozen=True)
class UsCsvResult:
    holdings: list[Holding] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    errors: list[CsvError] = field(default_factory=list)


def _dec(raw: str, what: str) -> Decimal:
    try:
        return parse_decimal(NAME, what, raw)
    except DataQualityError:
        raise ValueError(f"{what} {raw!r} is not a number") from None


def _date(raw: str, as_of: date) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            got = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if got > as_of:
            raise ValueError(f"acquired_on {raw!r} is in the future")
        return got
    raise ValueError(f"acquired_on {raw!r} must be YYYY-MM-DD or MM/DD/YYYY")


def _venue(symbol: str, raw: str, resolver: UsResolver) -> tuple[str, str | None]:
    """(exchange, name): the master's exchange wins; else the CSV's must be NYSE/NASDAQ/ARCA."""
    found = resolver.resolve_symbol(symbol, raw)
    if found is not None:
        venue = normalise_us_exchange(found.exchange)
        if venue is None:
            raise ValueError(f"{symbol} is listed on {found.exchange}; only {VENUES} are supported")
        return venue, found.name
    if not raw:
        raise ValueError(
            f"exchange is empty and {symbol} is not in the security master; add an exchange "
            "column or run `nivesh master build`"
        )
    venue = normalise_us_exchange(raw)
    if venue is None:
        raise ValueError(f"exchange {raw!r} must be one of {VENUES}")
    return venue, None


def _row(
    row: dict[str, str], resolver: UsResolver, as_of: date
) -> tuple[Holding, Lot | None] | None:
    label = row["account_label"]
    if not label:
        raise ValueError("account_label is empty")
    symbol = row["symbol"].upper()
    if not _SYMBOL.match(symbol):
        raise ValueError(f"symbol {row['symbol']!r} is not a valid US ticker")
    if (row["currency"] or "USD").upper() != "USD":
        raise ValueError("only USD is supported here")
    asset_class = (row["asset_class"] or "equity").lower()
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class {asset_class!r} must be one of {', '.join(ASSET_CLASSES)}")
    qty = _dec(row["quantity"], "quantity")
    if qty < 0:
        raise ValueError("quantity must not be negative")
    if not row["avg_cost"]:
        raise ValueError("avg_cost is required (a CSV has no market price)")
    cost = _dec(row["avg_cost"], "avg_cost")
    if cost < 0:
        raise ValueError("avg_cost must not be negative")
    acquired = _date(row["acquired_on"], as_of) if row["acquired_on"] else None
    exchange, name = _venue(symbol, row["exchange"], resolver)
    if qty == 0:
        return None
    holding = Holding(
        isin=None, symbol=symbol, exchange=exchange, name=name, asset_class=asset_class,
        quantity=qty, avg_cost=cost, price=cost, price_basis="avg_cost", value_inr=None,
        as_of=as_of, source="us_csv", source_label=label, holder_ref="", currency="USD",
    )  # fmt: skip
    lot = (
        Lot(
            symbol=symbol,
            exchange=exchange,
            currency="USD",
            acquired_on=acquired,
            quantity=qty,
            cost_per_unit=cost,
            source="us_csv",
            source_label=label,
        )  # fmt: skip
        if acquired
        else None
    )
    return holding, lot


def import_us_csv(text: str, resolver: UsResolver, as_of: date) -> UsCsvResult:
    """Validate every row; all problems are collected with 1-based file line numbers."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if header is None:
        return UsCsvResult(errors=[CsvError(1, "file is empty")])
    names = [h.strip().lower() for h in header]
    missing = [c for c in ("account_label", "symbol", "quantity", "avg_cost") if c not in names]
    if missing:
        return UsCsvResult(errors=[CsvError(1, f"missing column(s): {', '.join(missing)}")])
    holdings: list[Holding] = []
    lots: list[Lot] = []
    errors: list[CsvError] = []
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue
        row = dict.fromkeys(COLUMNS, "")
        row.update({n: c.strip() for n, c in zip(names, cells, strict=False) if n in row})
        try:
            got = _row(row, resolver, as_of)
        except ValueError as e:
            errors.append(CsvError(reader.line_num, str(e)))
        else:
            if got:
                holdings.append(got[0])
                if got[1]:
                    lots.append(got[1])
    return UsCsvResult(holdings, lots, errors)


def convert_us_preset(text: str, preset: str, label: str) -> str:
    """Broker export -> template CSV text (then validated by `import_us_csv`)."""
    if preset not in PRESETS:
        raise PresetError(f"unknown preset {preset!r}; available: {', '.join(known_us_presets())}")
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = [h.strip().lower() for h in next(reader, [])]
    pick: dict[str, int] = {}
    for col, candidates in PRESETS[preset].items():
        idx = next((header.index(c) for c in candidates if c in header), None)
        if idx is not None:
            pick[col] = idx
    need = [c for c in ("symbol", "quantity", "avg_cost") if c not in pick]
    if need:
        raise PresetError(
            f"line 1: the {preset} export is missing column(s) for: {', '.join(need)}"
        )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for cells in reader:
        if not any(c.strip() for c in cells):
            out.write("\n")  # keep line numbers aligned with the source file
            continue

        def get(col: str, cells: list[str] = cells) -> str:
            return cells[pick[col]].strip() if col in pick and pick[col] < len(cells) else ""

        writer.writerow(
            [label, get("symbol"), get("exchange"), get("quantity"), get("avg_cost"), "USD",
             "equity", get("acquired_on")]
        )  # fmt: skip
    return out.getvalue()


def known_us_presets() -> list[str]:
    return sorted(PRESETS)
