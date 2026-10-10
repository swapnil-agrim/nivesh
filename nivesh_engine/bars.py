"""The price bar the analysis engines read: adjusted OHLC and volume on one basis (ST-6.1).

Pure and Decimal-only. A stored bar carries a raw close and, when available, a split-adjusted
close; the engines never mix the two. `to_bars` rescales the open, high, low and close by one
factor per bar (adjusted close over close) and the volume by its inverse, so a split or bonus is
neither a price drop nor a volume spike. A bar with no adjustment information keeps its raw values
and the conversion says so. Dividends are not applied (price return, not total return).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.market_models import PriceBar
from nivesh_engine import dmath


@dataclass(frozen=True)
class Bar:
    date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: Decimal | None


def to_bars(
    bars: Sequence[PriceBar], factors: Mapping[date, Decimal] | None = None
) -> tuple[list[Bar], list[str]]:
    """(bars on the adjusted basis sorted by date, notes). `factors` (date -> adjusted close over
    close, unrounded) wins over the stored adjusted close, whose six-digit rounding limits the
    ratio to about 1e-6 relative precision on low-priced securities."""
    out: list[Bar] = []
    raw = 0
    with localcontext(dmath.CONTEXT):
        for b in sorted(bars, key=lambda x: x.date):
            if factors is not None and b.date in factors:
                f = factors[b.date]
            elif b.adj_close is not None and b.close != 0:
                f = b.adj_close / b.close
            else:
                f = dmath.ONE
                raw += 1
            out.append(
                Bar(
                    b.date,
                    None if b.open is None else b.open * f,
                    None if b.high is None else b.high * f,
                    None if b.low is None else b.low * f,
                    b.close * f,
                    None if b.volume is None else Decimal(b.volume) / f,
                )
            )
    notes = [f"adj_close missing on {raw} bar(s); close used"] if raw else []
    return out, notes


class BarDataError(ValueError):
    """The bars handed to an engine are not usable (unsorted dates or a duplicated date)."""


def check_bars(bars: Sequence[Bar]) -> None:
    """Raise BarDataError unless the dates strictly increase."""
    for prev, cur in zip(bars, bars[1:], strict=False):
        if cur.date <= prev.date:
            raise BarDataError(f"bar dates must strictly increase: {prev.date} then {cur.date}")
