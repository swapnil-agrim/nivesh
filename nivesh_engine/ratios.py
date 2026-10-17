"""Ratios from standardised statement rows (margins, ROE, leverage, growth). Pure, Decimal only.

A ratio whose inputs are missing is None and its inputs are listed; a zero denominator is None,
never an error and never a zero. For India (`market="IN"`) net margin and ROE use the profit
attributable to the parent where it can be derived, and `profit_basis` says which profit was used
(quarterly rows have no share count, so they stay consolidated and are labelled so).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from nivesh_engine.attributable import attributable_profit
from nivesh_engine.statements import PeriodType, StatementRow, latest_as_of

# ratio -> (numerator, denominator)
RATIOS: dict[str, tuple[str, str]] = {
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "roe": ("net_income", "total_equity"),
    "debt_to_equity": ("total_debt", "total_equity"),
}
_Q = Decimal("0.0001")


@dataclass
class RatioSet:
    period_end: date | None
    period_type: PeriodType
    values: dict[str, Decimal | None] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # input items absent for this period
    profit_basis: str | None = None  # India: the basis of the net income in net margin and ROE


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b).quantize(_Q)


def ratios(
    rows: Iterable[StatementRow],
    period_type: PeriodType = "A",
    as_of: date | None = None,
    *,
    market: str | None = None,
) -> RatioSet:
    """Ratios for the latest period of `period_type` known at `as_of`; growth is year over year
    (previous annual period, or the same quarter a year earlier)."""
    pit = [r for r in latest_as_of(rows, as_of) if r.period_type == period_type]
    if not pit:
        return RatioSet(None, period_type, {k: None for k in [*RATIOS, "revenue_growth"]})
    by_period: dict[date, dict[str, Decimal]] = {}
    for r in pit:
        by_period.setdefault(r.period_end, {})[r.item] = r.value
    end = max(by_period)
    cur = by_period[end]
    out = RatioSet(end, period_type)
    used: Mapping[str, Decimal | None] = cur  # the figures the numerators come from
    if market == "IN" and "net_income" in cur:
        profit = attributable_profit(
            cur["net_income"], minority_interest=cur.get("minority_interest"),
            eps=cur.get("eps"), shares_out=cur.get("shares_out"),
        )  # fmt: skip
        out.profit_basis, used = profit.basis, {**cur, "net_income": profit.value}
    missing: set[str] = set()
    for name, (num, den) in RATIOS.items():
        missing |= {i for i in (num, den) if i not in cur}
        out.values[name] = _div(used.get(num), cur.get(den))
    prior = [d for d in by_period if 350 <= (end - d).days <= 380]
    prev = by_period[prior[0]].get("revenue") if prior else None
    if "revenue" not in cur or prev is None:
        missing.add("revenue" if "revenue" not in cur else "revenue (prior year)")
        out.values["revenue_growth"] = None
    else:
        g = _div(cur["revenue"] - prev, prev)
        out.values["revenue_growth"] = g
    out.missing = sorted(missing)
    return out
