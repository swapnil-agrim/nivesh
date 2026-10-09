"""CSV import for accounts the other sources do not cover (ST-2.7).

Columns follow `templates/holdings.csv` (a documentation copy; this module never reads it). A CSV
has no market price, so price is the average cost and `price_basis` is `avg_cost` (book value).
Broker preset layouts are "as of writing, verify against a real export" (deferred D4).
"""

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_core.errors import NiveshError
from nivesh_core.holdings import Holding
from nivesh_core.security_resolver import SecurityResolver

COLUMNS = (
    "account_label",
    "isin",
    "symbol",
    "exchange",
    "quantity",
    "avg_cost",
    "currency",
    "asset_class",
)
ASSET_CLASSES = ("equity", "etf", "mf", "bond", "nps", "fd", "cash")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
NAME = "csv"

# Template column -> candidate headers in the broker export (case-insensitive, first match wins).
PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "zerodha": {
        "symbol": ("instrument", "symbol"),
        "isin": ("isin",),
        "quantity": ("qty.", "quantity available", "quantity"),
        "avg_cost": ("avg. cost", "average price"),
    },
    "groww": {
        "isin": ("isin",),
        "quantity": ("quantity",),
        "avg_cost": ("average buy price", "average price"),
    },
    "upstox": {
        "symbol": ("symbol", "trading symbol"),
        "isin": ("isin",),
        "quantity": ("quantity", "qty"),
        "avg_cost": ("avg price", "average price", "avg. price"),
    },
}


@dataclass(frozen=True)
class CsvError:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


@dataclass(frozen=True)
class CsvResult:
    holdings: list[Holding] = field(default_factory=list)
    errors: list[CsvError] = field(default_factory=list)


class PresetError(NiveshError):
    pass


def _dec(raw: str, what: str) -> Decimal:
    try:
        return parse_decimal(NAME, what, raw)
    except DataQualityError:
        raise ValueError(f"{what} {raw!r} is not a number") from None


def _holding(row: dict[str, str], resolver: SecurityResolver, as_of: date) -> Holding | None:
    label = row["account_label"]
    if not label:
        raise ValueError("account_label is empty")
    isin, symbol = row["isin"].upper(), row["symbol"]
    if not isin and not symbol:
        raise ValueError("row needs an isin or a symbol")
    if isin and not _ISIN.match(isin):
        raise ValueError(f"isin {isin!r} is not a valid ISIN")
    if (row["currency"] or "INR").upper() != "INR":
        raise ValueError("only INR holdings are supported here; foreign holdings arrive with E3")
    asset_class = (row["asset_class"] or "equity").lower()
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class {asset_class!r} must be one of {', '.join(ASSET_CLASSES)}")
    qty = _dec(row["quantity"], "quantity")
    if qty < 0:
        raise ValueError("quantity must not be negative")
    if row["avg_cost"]:
        cost: Decimal | None = _dec(row["avg_cost"], "avg_cost")
        if cost is not None and cost < 0:
            raise ValueError("avg_cost must not be negative")
    elif asset_class in ("cash", "fd"):
        cost = None  # the quantity is the amount itself
    else:
        raise ValueError("avg_cost is required (a CSV has no market price)")
    if qty == 0:
        return None
    price = cost if cost is not None else Decimal(1)
    found = resolver.resolve(isin) if isin else None
    exchange = row["exchange"].upper() or ("NSE" if asset_class in ("equity", "etf") else "MANUAL")
    if found:
        sym, exch, name = found.symbol, found.exchange, found.name
    elif symbol:
        sym, exch, name = symbol, exchange, None
    else:
        sym, exch, name = isin, "ISIN", None
    return Holding(
        isin=isin or None, symbol=sym, exchange=exch, name=name, asset_class=asset_class,
        quantity=qty, avg_cost=cost, price=price, price_basis="avg_cost", value_inr=qty * price,
        as_of=as_of, source="csv", source_label=label, holder_ref="",
        amfi_code=found.amfi_code if found else None,
        unresolved=not symbol and found is None,
    )  # fmt: skip


def import_csv(text: str, resolver: SecurityResolver, as_of: date) -> CsvResult:
    """Validate every row; all problems are collected with 1-based file line numbers."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    header = next(reader, None)
    if header is None:
        return CsvResult(errors=[CsvError(1, "file is empty")])
    names = [h.strip().lower() for h in header]
    missing = [c for c in ("account_label", "quantity") if c not in names]
    if "isin" not in names and "symbol" not in names:
        missing.append("isin or symbol")
    if missing:
        return CsvResult(errors=[CsvError(1, f"missing column(s): {', '.join(missing)}")])
    holdings: list[Holding] = []
    errors: list[CsvError] = []
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue
        row = dict.fromkeys(COLUMNS, "")
        row.update({n: c.strip() for n, c in zip(names, cells, strict=False) if n in row})
        try:
            h = _holding(row, resolver, as_of)
        except ValueError as e:
            errors.append(CsvError(reader.line_num, str(e)))
        else:
            if h:
                holdings.append(h)
    return CsvResult(holdings, errors)


def convert_preset(text: str, preset: str, label: str) -> str:
    """Broker export -> template CSV text (then validated by `import_csv`)."""
    if preset not in PRESETS:
        raise PresetError(f"unknown preset {preset!r}; available: {', '.join(sorted(PRESETS))}")
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    header = [h.strip().lower() for h in next(reader, [])]
    pick: dict[str, int] = {}
    for col, candidates in PRESETS[preset].items():
        idx = next((header.index(c) for c in candidates if c in header), None)
        if idx is not None:
            pick[col] = idx
    need = [c for c in ("quantity", "avg_cost") if c not in pick]
    if "isin" not in pick and "symbol" not in pick:
        need.append("isin or symbol")
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
            [
                label,
                get("isin"),
                get("symbol"),
                "",
                get("quantity"),
                get("avg_cost"),
                "INR",
                "equity",
            ]
        )
    return out.getvalue()


def known_presets() -> Sequence[str]:
    return sorted(PRESETS)
