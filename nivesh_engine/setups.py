"""ST-6.2 setup classifier: which chart setup a security is in, with entry zone, stop, invalidation
and reward-to-risk. Pure; every threshold comes from `SetupsSettings`, `LevelsSettings` and
`TaSettings`. A description of a chart pattern, not advice.

Setups (the first that matches in `cfg.precedence` wins; default breakout, pullback,
trend_continuation, base, downtrend, none):
  breakout: the last close is above the nearest resistance over the previous close, on volume at
    least `breakout_volume_mult` times the average of the 20 sessions before it.
  uptrend: close > SMA50 > SMA200 and the SMA200 is higher than `slope_lookback` sessions ago.
  pullback: uptrend, close within `pullback_atr_band` ATR of the SMA50 or the EMA, RSI below
    `rsi_trend_min`.
  trend_continuation: uptrend, RSI from `rsi_trend_min` to `rsi_trend_max` inclusive, and no
    breakout in the 10 sessions before the last.
  base: ATR over `base_lookback` sessions below `base_atr_pct_max` percent of the close, the close
    inside that lookback's high-low range and at or above the SMA200.
  downtrend: close < SMA50 < SMA200.
Entry zone (0.5 ATR wide): breakout from the broken level up; pullback centred on the SMA50;
continuation from half an ATR below the last close up to it; base from the range high up.
Stop = entry low - `stop_atr_mult` x ATR. Invalidation = the nearest support strictly below the
entry low, else the stop. Reward/risk = (nearest resistance above the entry zone - entry midpoint)
/ (entry midpoint - stop); unavailable, with a reason, when no resistance lies above (no target is
invented). Fewer than 200 bars: no setup, with a reason.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.analysis_config import LevelsSettings, SetupsSettings, TaSettings
from nivesh_engine import dmath, ta
from nivesh_engine.bars import Bar, check_bars
from nivesh_engine.levels import Level, Pivots, cluster_pivots, swing_pivots
from nivesh_engine.metric import Metric, na, ok

QUANTUM = Decimal("0.00000001")
ZONE_ATR = Decimal("0.5")
RECENT_BREAKOUT_BARS = 10
Zone = tuple[Decimal, Decimal]


@dataclass(frozen=True)
class Setup:
    as_of: date | None  # the last bar used
    setup: str | None  # None when the setup cannot be assessed (see `reason`)
    reason: str | None
    matched: tuple[str, ...]  # every setup that matched, in precedence order ("none" excluded)
    entry_low: Decimal | None
    entry_high: Decimal | None
    stop: Decimal | None
    invalidation: Decimal | None
    invalidation_basis: str | None  # "support" or "stop"
    target: Decimal | None  # nearest resistance above the entry zone
    reward_risk: Metric


def pick_invalidation(
    entry_low: Decimal, stop: Decimal, levels: Sequence[Level]
) -> tuple[Decimal, str]:
    """(price, basis): the nearest level strictly below the entry low, else the stop."""
    below = [lv.price for lv in levels if lv.price < entry_low]
    return (max(below), "support") if below else (stop, "stop")


def _unavailable(last: date | None, reason: str) -> Setup:
    return Setup(last, None, reason, (), None, None, None, None, None, None, na(reason))


def _broken_level(
    used: Sequence[Bar],
    known: int,
    pivots: Pivots,
    levels_cfg: LevelsSettings,
    cfg: SetupsSettings,
) -> Decimal | None:
    """The resistance the bar at index known - 1 closed above on strong volume, if any. Levels
    are those confirmed by the bars before it, so a series cut at that bar gives the same answer."""
    window = ta.VOLUME_WINDOW
    if known < window + 2:
        return None
    clusters = cluster_pivots(pivots, levels_cfg, bars_known=known - 1)
    above = [lv.price for lv in clusters if lv.price > used[known - 2].close]
    last = used[known - 1]
    if not above or last.close <= min(above):
        return None
    prior = [b.volume for b in used[known - 1 - window : known - 1]]
    if last.volume is None or any(v is None for v in prior):
        return None
    avg = dmath.mean([v for v in prior if v is not None])
    if avg <= 0 or last.volume < cfg.breakout_volume_mult * avg:
        return None
    return min(above)


def _base_zone(
    used: Sequence[Bar], close: Decimal, sma200: Decimal, half: Decimal, cfg: SetupsSettings
) -> Zone | None:
    lookback = cfg.base_lookback
    atr = ta.atr(used, lookback)
    window = used[-lookback:]
    if atr is None or close == 0 or any(b.high is None or b.low is None for b in window):
        return None
    top = max(b.high for b in window if b.high is not None)
    bottom = min(b.low for b in window if b.low is not None)
    tight = atr / close * dmath.HUNDRED < cfg.base_atr_pct_max
    if tight and close >= sma200 and bottom <= close <= top:
        return top, top + 2 * half
    return None


def classify_setup(
    bars: Sequence[Bar],
    *,
    as_of: date,
    cfg: SetupsSettings | None = None,
    levels_cfg: LevelsSettings | None = None,
    ta_cfg: TaSettings | None = None,
) -> Setup:
    """The setup at the last bar on or before `as_of`; see the module notes for the rules."""
    cfg, levels_cfg, ta_cfg = (
        cfg or SetupsSettings(),
        levels_cfg or LevelsSettings(),
        ta_cfg or TaSettings(),
    )
    check_bars(bars)
    used = [b for b in bars if b.date <= as_of]
    n = len(used)
    last_date = used[-1].date if used else None
    need = max(ta_cfg.min_bars_long, ta.LONG_SMA)
    if n < need:
        return _unavailable(last_date, f"need {need} bars, have {n}")
    closes = [b.close for b in used]
    close = closes[-1]
    atr = ta.atr(used, ta_cfg.atr_period)
    sma50, sma200 = ta.sma(closes, ta.SLOPE_SMA), ta.sma(closes, ta.LONG_SMA)
    if atr is None or sma50 is None or sma200 is None:
        return _unavailable(last_date, "ATR needs a high and a low on every bar of its window")

    with localcontext(dmath.CONTEXT):
        half = ZONE_ATR * atr / 2
        prior200 = ta.sma(closes[: -ta_cfg.slope_lookback], ta.LONG_SMA)
        uptrend = close > sma50 > sma200 and prior200 is not None and sma200 > prior200
        rsi = ta.rsi(closes, ta_cfg.rsi_period)
        ema = ta.ema(closes, ta_cfg.ema_window)
        pivots = swing_pivots(used, levels_cfg.pivot_window)
        pool = cluster_pivots(pivots, levels_cfg)

        hits: dict[str, Zone | None] = {}
        broken = _broken_level(used, n, pivots, levels_cfg, cfg)
        if broken is not None:
            hits["breakout"] = (broken, broken + 2 * half)
        if uptrend and rsi is not None:
            near = cfg.pullback_atr_band * atr
            if rsi < cfg.rsi_trend_min and (
                abs(close - sma50) <= near or (ema is not None and abs(close - ema) <= near)
            ):
                hits["pullback"] = (sma50 - half, sma50 + half)
            recent = range(n - RECENT_BREAKOUT_BARS, n)
            if cfg.rsi_trend_min <= rsi <= cfg.rsi_trend_max and not any(
                _broken_level(used, known, pivots, levels_cfg, cfg) is not None for known in recent
            ):
                hits["trend_continuation"] = (close - 2 * half, close)
        if n > cfg.base_lookback and (zone := _base_zone(used, close, sma200, half, cfg)):
            hits["base"] = zone
        if close < sma50 < sma200:
            hits["downtrend"] = None

        matched = tuple(name for name in cfg.precedence if name in hits)
        chosen = next((nm for nm in cfg.precedence if nm == "none" or nm in hits), "none")
        zone = hits.get(chosen)
        if zone is None:
            why = (
                "downtrend: no entry zone is defined"
                if chosen == "downtrend"
                else "no setup matched: no entry zone is defined"
            )
            return Setup(
                last_date, chosen, why, matched, None, None, None, None, None, None, na(why)
            )

        low, high = zone
        stop = low - cfg.stop_atr_mult * atr
        invalidation, basis = pick_invalidation(low, stop, pool)
        above = [lv.price for lv in pool if lv.price > high]
        target = min(above) if above else None
        mid = (low + high) / 2
        if target is None:
            rr = na("no resistance above the entry zone, so no target is set")
        elif mid - stop <= 0:
            rr = na("no risk distance between the entry and the stop")
        else:
            rr = ok(
                dmath.quantize((target - mid) / (mid - stop), QUANTUM),
                target=target,
                entry_mid=mid,
                stop=stop,
            )

        def q(v: Decimal) -> Decimal:
            return dmath.quantize(v, QUANTUM)

        return Setup(
            last_date,
            chosen,
            None,
            matched,
            q(low),
            q(high),
            q(stop),
            q(invalidation),
            basis,
            None if target is None else q(target),
            rr,
        )
