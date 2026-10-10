"""Fund overlap and look-through exposure (ST-5.5).

Pure and Decimal-only: no I/O, no clock. Inputs are the stored portfolio lines of each fund (latest
month), the funds' current INR values and the direct equity rows; the engine never reads a store.

- Pairwise overlap is the sum, over ISINs both funds hold as equity, of the smaller weight
  (percent). Cash, derivative, debt and nested-fund lines are `other` and take no part.
- Look-through exposure of a stock is `value_inr * weight_pct / 100` per fund, summed with any
  direct holding of the same ISIN; each contribution is listed. Percentages are of the portfolio
  total.
- A stock with no sector in the master is reported in `unmapped_sector`, never guessed. Funds held
  inside funds are not recursed: they sit in `other`.
- A fund with no current value or no stored holdings is excluded and named with the reason.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from nivesh_core.mf_models import FundHoldingRow

ZERO, HUNDRED = Decimal(0), Decimal(100)
CENT = Decimal("0.01")
TOP_STOCKS = 20


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Overlap:
    overlap_pct: Decimal
    common_isins: int


def _equity(rows: Sequence[FundHoldingRow]) -> dict[str, Decimal]:
    return {r.isin: r.weight_pct for r in rows if r.kind == "equity"}


def pairwise_overlap(a: Sequence[FundHoldingRow], b: Sequence[FundHoldingRow]) -> Overlap:
    wa, wb = _equity(a), _equity(b)
    common = sorted(wa.keys() & wb.keys())
    return Overlap(_q(sum((min(wa[i], wb[i]) for i in common), ZERO)), len(common))


@dataclass(frozen=True)
class OverlapMatrix:
    codes: list[str]
    cells: dict[tuple[str, str], Overlap]  # both orders present, diagonal omitted

    def get(self, a: str, b: str) -> Overlap:
        return self.cells[(a, b)]


def overlap_matrix(funds: Mapping[str, Sequence[FundHoldingRow]]) -> OverlapMatrix:
    """Symmetric matrix over the funds, sorted by AMFI code."""
    codes = sorted(funds)
    cells: dict[tuple[str, str], Overlap] = {}
    for i, a in enumerate(codes):
        for b in codes[i + 1 :]:
            cells[(a, b)] = cells[(b, a)] = pairwise_overlap(funds[a], funds[b])
    return OverlapMatrix(codes, cells)


@dataclass(frozen=True)
class OwnedFund:
    amfi_code: str
    name: str
    value_inr: Decimal | None


@dataclass(frozen=True)
class DirectEquity:
    isin: str
    name: str
    value_inr: Decimal | None


@dataclass(frozen=True)
class Contribution:
    source: str  # "direct" or the fund's AMFI code
    name: str
    exposure_inr: Decimal


@dataclass(frozen=True)
class StockExposure:
    isin: str
    sector: str | None
    exposure_inr: Decimal
    exposure_pct: Decimal
    contributions: list[Contribution]


@dataclass(frozen=True)
class SectorExposure:
    sector: str
    exposure_inr: Decimal
    exposure_pct: Decimal


@dataclass(frozen=True)
class Excluded:
    amfi_code: str
    name: str
    reason: str


@dataclass(frozen=True)
class LookThrough:
    total_inr: Decimal
    stocks: list[StockExposure]  # top 20 by exposure
    stock_count: int
    sectors: list[SectorExposure]
    unmapped_sector_inr: Decimal
    unmapped_sector_pct: Decimal
    other_inr: Decimal
    other_pct: Decimal
    excluded: list[Excluded] = field(default_factory=list)
    month_ends: dict[str, date] = field(default_factory=dict)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    return _q(HUNDRED * part / total)


def look_through(
    funds: Sequence[OwnedFund],
    fund_holdings: Mapping[str, Sequence[FundHoldingRow]],
    sector_of: Mapping[str, str | None],
    direct: Sequence[DirectEquity],
    total: Decimal,
) -> LookThrough:
    """Exposure per stock and sector in rupees and percent of `total`, fund lines plus direct."""
    if total <= 0:
        raise ValueError("portfolio total must be > 0")
    stock: dict[str, list[Contribution]] = defaultdict(list)
    other = ZERO
    excluded: list[Excluded] = []
    months: dict[str, date] = {}
    for f in sorted(funds, key=lambda x: x.amfi_code):
        rows = fund_holdings.get(f.amfi_code, ())
        if f.value_inr is None:
            excluded.append(Excluded(f.amfi_code, f.name, "current value unavailable"))
            continue
        if not rows:
            excluded.append(Excluded(f.amfi_code, f.name, "no stored holdings"))
            continue
        months[f.amfi_code] = max(r.month_end for r in rows)
        for r in rows:
            amount = f.value_inr * r.weight_pct / HUNDRED
            if r.kind == "equity":
                stock[r.isin].append(Contribution(f.amfi_code, f.name, amount))
            else:
                other += amount
    for d in sorted(direct, key=lambda x: x.isin):
        if d.value_inr is not None:
            stock[d.isin].append(Contribution("direct", d.name, d.value_inr))
    sums = {isin: sum((c.exposure_inr for c in cs), ZERO) for isin, cs in stock.items()}
    ranked = sorted(sums, key=lambda i: (-sums[i], i))
    stocks = [
        StockExposure(
            i, sector_of.get(i), _q(sums[i]), _pct(sums[i], total),
            [Contribution(c.source, c.name, _q(c.exposure_inr)) for c in stock[i]],
        )
        for i in ranked[:TOP_STOCKS]
    ]  # fmt: skip
    by_sector: dict[str, Decimal] = defaultdict(lambda: ZERO)
    unmapped = ZERO
    for i, amount in sums.items():
        sector = sector_of.get(i)
        if sector:
            by_sector[sector] += amount
        else:
            unmapped += amount
    sectors = [
        SectorExposure(s, _q(v), _pct(v, total))
        for s, v in sorted(by_sector.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return LookThrough(
        total, stocks, len(sums), sectors, _q(unmapped), _pct(unmapped, total), _q(other),
        _pct(other, total), excluded, months,
    )  # fmt: skip
