from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import RegimeSettings, SetupsSettings
from nivesh_engine import regime, setups
from nivesh_engine.bars import Bar
from nivesh_engine.levels import Level
from tests.analysis_fx import (
    bars_from_closes,
    day,
    legs_closes,
    oscillating_closes,
    ramp_closes,
)

D = Decimal
FAR = date(2030, 1, 1)


def near(a: Decimal | None, b: str | Decimal, tol: str = "1e-6") -> bool:
    assert a is not None
    return abs(a - D(b)) <= D(tol)


# ---- regime --------------------------------------------------------------------------------


def rising(n: int = 260) -> list[Bar]:
    return bars_from_closes(ramp_closes(n))


def falling(n: int = 260) -> list[Bar]:
    return bars_from_closes(ramp_closes(n, start=500, step=-1))


def universe(up: int, down: int, short: int = 0) -> dict[int, list[Bar]]:
    out: dict[int, list[Bar]] = {}
    for i in range(up):
        out[i] = rising()
    for i in range(down):
        out[100 + i] = falling()
    for i in range(short):
        out[200 + i] = rising(100)
    return out


def test_risk_on_when_index_above_200dma_and_breadth_at_least_threshold() -> None:
    got = regime.market_regime(rising(), universe(15, 10), as_of=FAR)
    assert got.label == "risk_on" and got.reason is None
    assert got.index_above_sma200 is True
    assert got.breadth.pct == D(60) and got.breadth.above == 15 and got.breadth.counted == 25


def test_risk_off_when_index_below_and_breadth_at_most_threshold() -> None:
    got = regime.market_regime(falling(), universe(10, 15), as_of=FAR)
    assert got.label == "risk_off" and got.index_above_sma200 is False
    assert got.breadth.pct == D(40)


def test_neutral_when_signals_disagree() -> None:
    up_index_weak_breadth = regime.market_regime(rising(), universe(5, 20), as_of=FAR)
    assert up_index_weak_breadth.label == "neutral"
    down_index_strong_breadth = regime.market_regime(falling(), universe(20, 5), as_of=FAR)
    assert down_index_strong_breadth.label == "neutral"
    middling = regime.market_regime(rising(), universe(12, 13), as_of=FAR)  # 48 percent
    assert middling.label == "neutral"


def test_regime_none_with_reason_without_enough_index_bars() -> None:
    got = regime.market_regime(rising(150), universe(15, 10), as_of=FAR)
    assert got.label is None and got.index_above_sma200 is None
    assert got.reason == "need 200 index bars, have 150"
    assert regime.market_regime([], universe(15, 10), as_of=FAR).label is None


def test_regime_none_when_universe_smaller_than_min_breadth_universe() -> None:
    got = regime.market_regime(rising(), universe(10, 9), as_of=FAR)
    assert got.label is None
    assert got.reason is not None and "breadth" in got.reason and "19" in got.reason
    assert got.index_above_sma200 is True  # the index signal is still reported


def test_breadth_counts_only_securities_with_200_bars_and_reports_the_denominator() -> None:
    got = regime.breadth(universe(12, 10, short=8), as_of=FAR)
    assert (got.above, got.counted, got.universe) == (12, 22, 30)
    assert got.pct is not None and near(got.pct, D(1200) / 22)
    assert regime.breadth({}, as_of=FAR).pct is None
    assert regime.breadth(universe(0, 0, short=5), as_of=FAR).pct is None


def test_regime_thresholds_come_from_config() -> None:
    cfg = RegimeSettings(risk_on_breadth_pct=D(70), risk_off_breadth_pct=D(30))
    assert regime.market_regime(rising(), universe(15, 10), as_of=FAR, cfg=cfg).label == "neutral"
    loose = RegimeSettings(
        risk_on_breadth_pct=D(50), risk_off_breadth_pct=D(30), min_breadth_universe=5
    )
    assert regime.market_regime(rising(), universe(3, 2), as_of=FAR, cfg=loose).label == "risk_on"


def test_regime_ignores_bars_after_as_of() -> None:
    assert regime.market_regime(rising(), universe(15, 10), as_of=day(150)).label is None


# ---- setups: golden cases ------------------------------------------------------------------


def classify(bars: list[Bar], **kw: object) -> setups.Setup:
    return setups.classify_setup(bars, as_of=FAR, **kw)  # type: ignore[arg-type]


def continuation_bars() -> list[Bar]:
    # up 2, down 1, repeated: close 250, SMA50 238.5, SMA200 201, RSI 65, ATR 10
    return bars_from_closes(legs_closes(100, [(1, "2"), (1, "-1")] * 150), spread=10)


def pullback_bars() -> list[Bar]:
    # a 250-bar climb to 350 then 10 bars of -1.5: close 335, SMA50 332.75, RSI about 38, ATR 12
    return bars_from_closes(legs_closes(100, [(250, "1"), (10, "-1.5")]), spread=12)


def breakout_bars() -> list[Bar]:
    # climb to 300 (swing high 307), pull back to 270 (swing low 263), climb to 303, then 308 on
    # three times the usual volume; ATR 14
    closes = legs_closes(100, [(200, "1"), (30, "-1"), (33, "1"), (1, "5")])
    return bars_from_closes(closes, spread=14, volumes={len(closes) - 1: 3000})


def base_bars() -> list[Bar]:
    return bars_from_closes(oscillating_closes(200, 1, 300), spread=4)


def test_golden_trend_continuation() -> None:
    got = classify(continuation_bars())
    assert got.setup == "trend_continuation" and got.reason is None
    assert got.matched == ("trend_continuation",)
    assert (got.entry_low, got.entry_high) == (D(245), D(250))  # last close down 0.5 ATR
    assert got.stop == D(225)  # entry low - 2 x ATR
    assert got.invalidation == D(225) and got.invalidation_basis == "stop"


def test_golden_pullback() -> None:
    got = classify(pullback_bars())
    assert got.setup == "pullback" and got.matched == ("pullback",)
    assert (got.entry_low, got.entry_high) == (D("329.75"), D("335.75"))  # SMA50 +- 0.25 ATR
    assert got.stop == D("305.75")


def test_golden_breakout() -> None:
    got = classify(breakout_bars())
    assert got.setup == "breakout" and got.matched == ("breakout",)
    assert (got.entry_low, got.entry_high) == (D(307), D(314))  # broken level up 0.5 ATR
    assert got.stop == D(279)


def test_golden_base() -> None:
    got = classify(base_bars())
    assert got.setup == "base" and got.matched == ("base",)
    assert (got.entry_low, got.entry_high) == (D(203), D(205))  # above the 40-bar range high
    assert got.stop == D(195)


def test_golden_downtrend() -> None:
    got = classify(bars_from_closes(legs_closes(400, [(300, "-1")]), spread=10))
    assert got.setup == "downtrend" and got.matched == ("downtrend",)


def test_golden_none() -> None:
    got = classify(bars_from_closes(oscillating_closes(200, 5, 301), spread=10))
    assert got.setup == "none" and got.matched == ()


def test_stop_is_entry_low_minus_2_atr_and_multiplier_comes_from_config() -> None:
    assert classify(continuation_bars()).stop == D(225)
    wide = classify(continuation_bars(), cfg=SetupsSettings(stop_atr_mult=D(3)))
    assert wide.stop == D(215)


def test_invalidation_is_nearest_support_below_entry_else_the_stop() -> None:
    got = classify(breakout_bars())
    assert got.invalidation == D(263) and got.invalidation_basis == "support"  # the swing low
    pool = [Level(D(90), 1), Level(D(95), 2), Level(D(100), 3), Level(D(110), 1)]
    assert setups.pick_invalidation(D(100), D(80), pool) == (D(95), "support")
    assert setups.pick_invalidation(D(100), D(80), [Level(D(110), 1)]) == (D(80), "stop")
    assert setups.pick_invalidation(D(100), D(80), []) == (D(80), "stop")


def test_reward_risk_known_answer() -> None:
    got = classify(pullback_bars())
    assert got.target == D(356)  # the swing high (peak close 350 + 6)
    # entry mid 332.75, stop 305.75: (356 - 332.75) / (332.75 - 305.75)
    assert got.reward_risk.available and near(got.reward_risk.value, D("23.25") / D("27"))


def test_reward_risk_none_with_reason_without_resistance_above() -> None:
    for bars in (continuation_bars(), breakout_bars(), base_bars()):
        got = classify(bars)
        assert got.target is None
        assert not got.reward_risk.available and got.reward_risk.value is None
        assert got.reward_risk.reason is not None and "resistance" in got.reward_risk.reason


def test_precedence_breakout_beats_trend_continuation_when_both_match() -> None:
    both = SetupsSettings(rsi_trend_max=D(100))
    got = classify(breakout_bars(), cfg=both)
    assert got.setup == "breakout" and got.matched == ("breakout", "trend_continuation")
    flipped = SetupsSettings(
        rsi_trend_max=D(100),
        precedence=["trend_continuation", "breakout", "pullback", "base", "downtrend", "none"],
    )
    again = classify(breakout_bars(), cfg=flipped)
    assert again.setup == "trend_continuation"
    assert again.matched == ("trend_continuation", "breakout")


def test_downtrend_and_none_have_no_entry_zone_with_reason() -> None:
    for bars, word in (
        (bars_from_closes(legs_closes(400, [(300, "-1")]), spread=10), "downtrend"),
        (bars_from_closes(oscillating_closes(200, 5, 301), spread=10), "no setup"),
    ):
        got = classify(bars)
        assert got.entry_low is None and got.entry_high is None and got.stop is None
        assert got.invalidation is None and got.target is None
        assert got.reason is not None and word in got.reason
        assert not got.reward_risk.available


def test_setup_unavailable_with_fewer_than_200_bars() -> None:
    got = classify(bars_from_closes(ramp_closes(199), spread=10))
    assert got.setup is None and got.reason == "need 200 bars, have 199"
    assert got.matched == () and not got.reward_risk.available
    assert classify([]).setup is None
    assert classify(bars_from_closes(ramp_closes(200), spread=10)).setup is not None


def test_setup_ignores_bars_after_as_of_and_needs_atr_inputs() -> None:
    bars = breakout_bars()
    cut = setups.classify_setup(bars, as_of=day(len(bars) - 2))
    assert cut.setup != "breakout" and cut.as_of == day(len(bars) - 2)
    bare = [Bar(b.date, None, None, None, b.close, b.volume) for b in bars]
    got = classify(bare)
    assert got.setup is None and got.reason is not None and "high" in got.reason


def test_setup_output_is_deterministic() -> None:
    bars = pullback_bars()
    assert classify(bars) == classify(bars)
