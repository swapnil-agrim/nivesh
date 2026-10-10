"""ST-6.2 support and resistance levels from confirmed swing pivots, and unfilled gap zones.

Pure and Decimal-only. Rules (thresholds come from `LevelsSettings`):
  A swing high at bar i is the first maximum of the highs in bars i-w .. i+w; a swing low is the
  first minimum of the lows. The last w bars cannot be confirmed, so they are never pivots.
  Pivot prices (highs and lows together, a broken level turns from one into the other) are grouped
  by ascending-price agglomeration: a price joins the open cluster when it is within the tolerance
  percent of the cluster mean. A level is the cluster mean; its touches are the pivots in it.
  Support is a level at or below the last close, resistance a level above it. Each side keeps at
  most `max_levels`, ranked by touches (more first), then closeness to the last close, then price.
  A gap zone is the empty range between one bar's high and the next bar's low (up) or the reverse
  (down), at least `gap_min_pct` wide; it is kept until a later bar trades back through it.
A bar without a high or low uses its close instead.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.analysis_config import LevelsSettings
from nivesh_engine import dmath
from nivesh_engine.bars import Bar

QUANTUM = Decimal("0.00000001")
NO_PIVOTS = "no confirmed swing pivots"


@dataclass(frozen=True)
class Pivot:
    index: int
    date: date
    price: Decimal


@dataclass(frozen=True)
class Pivots:
    highs: list[Pivot]
    lows: list[Pivot]


@dataclass(frozen=True)
class Level:
    price: Decimal
    touches: int


@dataclass(frozen=True)
class GapZone:
    direction: str  # "up" or "down"
    low: Decimal
    high: Decimal
    date: date  # the bar after the gap opened


@dataclass(frozen=True)
class Levels:
    support: list[Level]
    resistance: list[Level]
    gaps: list[GapZone]
    reason: str | None


def _high(b: Bar) -> Decimal:
    return b.close if b.high is None else b.high


def _low(b: Bar) -> Decimal:
    return b.close if b.low is None else b.low


def swing_pivots(bars: Sequence[Bar], window: int) -> Pivots:
    """Confirmed swing highs and lows; ties go to the first bar."""
    highs = [_high(b) for b in bars]
    lows = [_low(b) for b in bars]
    out_h: list[Pivot] = []
    out_l: list[Pivot] = []
    for i in range(window, len(bars) - window):
        span = range(i - window, i + window + 1)
        top = max(highs[j] for j in span)
        if highs[i] == top and all(highs[j] < top for j in range(i - window, i)):
            out_h.append(Pivot(i, bars[i].date, top))
        bottom = min(lows[j] for j in span)
        if lows[i] == bottom and all(lows[j] > bottom for j in range(i - window, i)):
            out_l.append(Pivot(i, bars[i].date, bottom))
    return Pivots(out_h, out_l)


def cluster_prices(prices: Sequence[Decimal], tolerance_pct: Decimal) -> list[Level]:
    """Ascending-price agglomeration; each level is the mean of its prices."""
    clusters: list[list[Decimal]] = []
    with localcontext(dmath.CONTEXT):
        for price in sorted(prices):
            if clusters:
                mean = sum(clusters[-1], dmath.ZERO) / len(clusters[-1])
                gap = abs(price - mean)
                if gap == 0 or (mean != 0 and gap / abs(mean) * dmath.HUNDRED <= tolerance_pct):
                    clusters[-1].append(price)
                    continue
            clusters.append([price])
        return [
            Level(dmath.quantize(sum(c, dmath.ZERO) / len(c), QUANTUM), len(c)) for c in clusters
        ]


def rank_levels(
    levels: Sequence[Level], close: Decimal, max_levels: int
) -> tuple[list[Level], list[Level]]:
    """(support, resistance): at most `max_levels` per side, best first."""

    def best(side: list[Level]) -> list[Level]:
        ranked = sorted(side, key=lambda lv: (-lv.touches, abs(lv.price - close), lv.price))
        return ranked[:max_levels]

    below = [lv for lv in levels if lv.price <= close]
    above = [lv for lv in levels if lv.price > close]
    return best(below), best(above)


def cluster_pivots(
    pivots: Pivots, cfg: LevelsSettings, *, bars_known: int | None = None
) -> list[Level]:
    """Levels from pivots; with `bars_known`, only pivots a series of that many bars would have
    confirmed (so one pass over the full series serves every earlier as-of point)."""
    last = None if bars_known is None else bars_known - 1 - cfg.pivot_window
    chosen = [p.price for p in (*pivots.highs, *pivots.lows) if last is None or p.index <= last]
    return cluster_prices(chosen, cfg.cluster_tolerance_pct)


def cluster_levels(bars: Sequence[Bar], cfg: LevelsSettings) -> list[Level]:
    """Every level (clustered pivot prices), lowest price first; empty without pivots."""
    return cluster_pivots(swing_pivots(bars, cfg.pivot_window), cfg)


def gap_zones(bars: Sequence[Bar], cfg: LevelsSettings) -> list[GapZone]:
    """Unfilled gap zones, newest first, at most `max_gap_zones`."""
    zones: list[GapZone] = []
    with localcontext(dmath.CONTEXT):
        for i in range(1, len(bars)):
            prev_high, prev_low = _high(bars[i - 1]), _low(bars[i - 1])
            high, low = _high(bars[i]), _low(bars[i])
            later = bars[i + 1 :]
            if low > prev_high and prev_high > 0:
                size = (low - prev_high) / prev_high * dmath.HUNDRED
                if size >= cfg.gap_min_pct and all(_low(b) > prev_high for b in later):
                    zones.append(GapZone("up", prev_high, low, bars[i].date))
            elif high < prev_low and prev_low > 0:
                size = (prev_low - high) / prev_low * dmath.HUNDRED
                if size >= cfg.gap_min_pct and all(_high(b) < prev_low for b in later):
                    zones.append(GapZone("down", high, prev_low, bars[i].date))
    return list(reversed(zones))[: cfg.max_gap_zones]


def find_levels(bars: Sequence[Bar], cfg: LevelsSettings | None = None) -> Levels:
    """Support, resistance and unfilled gaps around the last close."""
    cfg = cfg or LevelsSettings()
    gaps = gap_zones(bars, cfg)
    pool = cluster_levels(bars, cfg)
    if not pool or not bars:
        return Levels([], [], gaps, NO_PIVOTS)
    support, resistance = rank_levels(pool, bars[-1].close, cfg.max_levels)
    return Levels(support, resistance, gaps, None)
