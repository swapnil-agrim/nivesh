"""Portfolio valuation lens for a fund (ST-5.4): weighted P/E and P/B of its holdings and the
current multiple against the fund's own history.

Pure and Decimal-only: no I/O, no clock. Per-stock multiples are inputs (the caller builds them
from stored statements and prices as of each month end). The weighted multiple is a weighted
harmonic mean, which is the inverse of the weighted earnings (or book) yield, so a loss-making or
missing stock cannot poison the average: holdings without a positive multiple are excluded and the
share of equity weight that was covered is reported. Too little coverage or history is
"unavailable" with a reason, never zero.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from nivesh_core.mf_models import FundHoldingRow
from nivesh_engine import dmath
from nivesh_engine.statements import StatementRow, latest_as_of

ZERO, HUNDRED = Decimal(0), Decimal(100)
PCT = Decimal("0.01")
MULT = Decimal("0.0001")
WINDOW_MONTHS = 36
MIN_MONTHS = 24


@dataclass(frozen=True)
class MultipleResult:
    value: Decimal | None
    coverage_pct: Decimal | None
    used: int
    total: int
    reason: str | None = None


def weighted_multiple(
    rows: Sequence[FundHoldingRow],
    multiples: Mapping[str, Decimal | None],
    min_coverage_pct: Decimal,
) -> MultipleResult:
    """Weighted harmonic mean of a multiple over the fund's equity holdings that have one."""
    equity = [r for r in rows if r.kind == "equity"]
    total_weight = sum((r.weight_pct for r in equity), ZERO)
    if not equity or total_weight == 0:
        return MultipleResult(None, None, 0, len(equity), "no equity holdings")
    usable = [(r.weight_pct, multiples.get(r.isin)) for r in equity]
    covered = [(w, m) for w, m in usable if m is not None and m > 0 and w > 0]
    weight = sum((w for w, _ in covered), ZERO)
    coverage = (HUNDRED * weight / total_weight).quantize(PCT, rounding=ROUND_HALF_EVEN)
    if not covered:
        return MultipleResult(None, coverage, 0, len(equity), "no holding has a positive multiple")
    if coverage < min_coverage_pct:
        why = f"multiples cover {coverage}% of equity weight (minimum {min_coverage_pct}%)"
        return MultipleResult(None, coverage, len(covered), len(equity), why)
    with localcontext() as ctx:
        ctx.prec = 28
        inverse = sum((w / m for w, m in covered if m is not None), ZERO)
        value = (weight / inverse).quantize(MULT, rounding=ROUND_HALF_EVEN)
    return MultipleResult(value, coverage, len(covered), len(equity))


@dataclass(frozen=True)
class HistoryCompare:
    current: Decimal | None
    median: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    months_used: int
    ratio: Decimal | None  # current / own median
    label: str | None  # "expensive", "cheap" or "in range" against the owner-set ratio
    reason: str | None = None


def history_compare(
    history: Mapping[date, Decimal | None],
    current: Decimal | None,
    stretch_ratio: Decimal,
    *,
    window: int = WINDOW_MONTHS,
    min_months: int = MIN_MONTHS,
) -> HistoryCompare:
    """The current multiple against the median, min and max of the latest `window` month ends in
    `history` (current excluded); fewer than `min_months` usable values is unavailable."""
    recent = [history[d] for d in sorted(history)[-window:]]
    values = [v for v in recent if v is not None and v > 0]
    if len(values) < min_months:
        why = f"only {len(values)} usable month(s) of history (need {min_months})"
        return HistoryCompare(current, None, None, None, len(values), None, None, why)
    med = dmath.median(values)
    stats = (med.quantize(MULT), min(values).quantize(MULT), max(values).quantize(MULT))
    if current is None or current <= 0:
        return HistoryCompare(
            current, *stats, len(values), None, None, "current multiple unavailable"
        )
    ratio = (current / med).quantize(MULT, rounding=ROUND_HALF_EVEN)
    label = (
        "expensive"
        if ratio > stretch_ratio
        else "cheap"
        if ratio * stretch_ratio < 1
        else "in range"
    )
    return HistoryCompare(current, *stats, len(values), ratio, label)


def _latest(by_period: dict[date, dict[str, Decimal]], *items: str) -> dict[str, Decimal] | None:
    for end in sorted(by_period, reverse=True):
        if all(i in by_period[end] for i in items):
            return by_period[end]
    return None


def stock_multiples(
    rows: Sequence[StatementRow], price: Decimal | None, as_of: date
) -> tuple[Decimal | None, Decimal | None]:
    """(P/E, P/B) of one stock from annual statements filed by `as_of` (no look-ahead).

    P/E is price over the latest annual EPS, P/B is price over equity per share (equity over
    shares outstanding). A missing input, a non-positive denominator or no price gives None.
    """
    if price is None or price <= 0:
        return None, None
    by_period: dict[date, dict[str, Decimal]] = {}
    for r in latest_as_of(rows, as_of):
        if r.period_type == "A":
            by_period.setdefault(r.period_end, {})[r.item] = r.value
    eps = _latest(by_period, "eps")
    book = _latest(by_period, "total_equity", "shares_out")
    pe = price / eps["eps"] if eps and eps["eps"] > 0 else None
    pb = None
    if book and book["shares_out"] > 0 and book["total_equity"] > 0:
        pb = price / (book["total_equity"] / book["shares_out"])
    return (
        None if pe is None else pe.quantize(MULT, rounding=ROUND_HALF_EVEN),
        None if pb is None else pb.quantize(MULT, rounding=ROUND_HALF_EVEN),
    )
