"""ST-6.2 market regime from the index trend and the breadth of the universe. Pure.

Rules (thresholds come from `RegimeSettings`):
  Breadth is the share, in percent, of securities with at least 200 bars (up to the as-of date)
  whose last close is above their own 200-session average; securities with fewer bars are not
  counted, and both the counted number and the universe size are reported.
  The regime is `risk_on` when the index closes above its 200-session average and breadth is at
  least `risk_on_breadth_pct`, `risk_off` when it closes below and breadth is at most
  `risk_off_breadth_pct`, and `neutral` otherwise (the signals disagree or breadth is in between).
  When the index has too few bars, or too few securities could be counted, there is no regime and
  a reason; a regime is never guessed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import RegimeSettings
from nivesh_engine import dmath
from nivesh_engine.bars import Bar
from nivesh_engine.ta import LONG_SMA, sma

QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class Breadth:
    pct: Decimal | None
    above: int
    counted: int
    universe: int


@dataclass(frozen=True)
class Regime:
    label: str | None
    reason: str | None
    index_above_sma200: bool | None
    breadth: Breadth


def _above_sma200(bars: Sequence[Bar], as_of: date) -> bool | None:
    closes = [b.close for b in bars if b.date <= as_of]
    avg = sma(closes, LONG_SMA)
    return None if avg is None else closes[-1] > avg


def breadth(universe: Mapping[int, Sequence[Bar]], *, as_of: date) -> Breadth:
    """Share of the securities with 200 bars whose last close is above their 200-session average."""
    states = [_above_sma200(bars, as_of) for _, bars in sorted(universe.items())]
    counted = [s for s in states if s is not None]
    above = sum(1 for s in counted if s)
    pct = dmath.quantize(dmath.HUNDRED * above / len(counted), QUANTUM) if counted else None
    return Breadth(pct, above, len(counted), len(universe))


def market_regime(
    index_bars: Sequence[Bar],
    universe: Mapping[int, Sequence[Bar]],
    *,
    as_of: date,
    cfg: RegimeSettings | None = None,
) -> Regime:
    """risk_on, risk_off or neutral for the market, or None with a reason."""
    cfg = cfg or RegimeSettings()
    wide = breadth(universe, as_of=as_of)
    above = _above_sma200(index_bars, as_of)
    if above is None:
        have = sum(1 for b in index_bars if b.date <= as_of)
        return Regime(None, f"need {LONG_SMA} index bars, have {have}", None, wide)
    if wide.pct is None or wide.counted < cfg.min_breadth_universe:
        why = (
            f"breadth needs {cfg.min_breadth_universe} securities with {LONG_SMA} bars, "
            f"have {wide.counted}"
        )
        return Regime(None, why, above, wide)
    if above and wide.pct >= cfg.risk_on_breadth_pct:
        label = "risk_on"
    elif not above and wide.pct <= cfg.risk_off_breadth_pct:
        label = "risk_off"
    else:
        label = "neutral"
    return Regime(label, None, above, wide)
