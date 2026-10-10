from datetime import date
from decimal import Decimal

import pytest

from nivesh_core.analysis_config import TaSettings
from nivesh_engine import dmath, ta
from nivesh_engine.bars import Bar, BarDataError
from tests.analysis_fx import bars_from_closes, day, lcg_bars, ramp_closes

D = Decimal
TOL = D("1e-9")


def near(a: Decimal | None, b: str | Decimal | int, tol: Decimal = TOL) -> bool:
    assert a is not None
    return abs(a - D(b)) <= tol


def val(res: ta.TaResult, name: str) -> Decimal:
    m = res.values[name]
    assert m.available, (name, m.reason)
    assert m.value is not None
    return m.value


def ramp_bars(n: int) -> list[Bar]:
    return bars_from_closes(ramp_closes(n))


def run(bars: list[Bar], **kw: object) -> ta.TaResult:
    return ta.ta_compute(bars, as_of=kw.pop("as_of", date(2030, 1, 1)), **kw)  # type: ignore[arg-type]


# ---- primitives ----------------------------------------------------------------------------


def test_sma_known_answer_window_3() -> None:
    xs = [D(v) for v in (1, 2, 3, 4, 5)]
    assert ta.sma(xs, 3) == D(4)
    assert ta.sma(xs, 6) is None
    assert ta.sma_series(xs, 3) == [D(2), D(3), D(4)]


def test_ema_seeded_with_sma_known_answer() -> None:
    xs = [D(v) for v in (1, 2, 3, 4, 5)]
    assert ta.ema_series(xs, 3) == [D(2), D(3), D(4)]
    assert ta.ema(xs, 3) == D(4)
    assert ta.ema(xs[:2], 3) is None


def test_rsi_wilder_known_answer_77_5862068965517() -> None:
    xs = [D(v) for v in (10, 11, 10, 12, 13, 12, 14)]
    assert near(ta.rsi(xs, 3), "77.5862068965517", D("1e-12"))
    assert ta.rsi(xs[:3], 3) is None  # needs n + 1 closes


def test_rsi_all_gains_is_100_all_losses_is_0() -> None:
    assert ta.rsi([D(v) for v in range(1, 12)], 3) == D(100)
    assert ta.rsi([D(v) for v in range(12, 1, -1)], 3) == D(0)


def test_rsi_flat_prices_is_unavailable_with_reason() -> None:
    assert ta.rsi([D(5)] * 20, 14) is None
    res = run(bars_from_closes([D(5)] * 30))
    m = res.values["rsi_14"]
    assert not m.available and m.value is None
    assert m.reason is not None and "no price change" in m.reason


def test_atr_wilder_known_answer_20_over_9() -> None:
    hlc = [(11, 9, 10), (12, 10, 11), (13, 10, 12), (14, 12, 13), (15, 13, 14)]
    bars = [Bar(day(i), None, D(h), D(low), D(c), None) for i, (h, low, c) in enumerate(hlc)]
    assert near(ta.atr(bars, 3), D(20) / D(9), D("1e-20"))
    assert ta.atr(bars[:3], 3) is None
    no_range = [Bar(b.date, None, None, None, b.close, None) for b in bars]
    assert ta.atr(no_range, 3) is None


def test_macd_line_signal_histogram_known_answer() -> None:
    xs = [D(v) for v in range(1, 7)]
    assert ta.macd(xs, 2, 3, 2) == (D("0.5"), D("0.5"), D(0))
    assert ta.macd(xs[:3], 2, 3, 2) is None


def test_roc_known_answer() -> None:
    assert ta.roc([D(100), D(110), D(121)], 2) == D("0.21")
    assert ta.roc([D(100), D(110)], 2) is None
    assert ta.roc([D(0), D(1), D(2)], 2) is None  # undefined base


def test_bollinger_width_known_answer_1_632993161855452() -> None:
    got = ta.bollinger_width([D(1), D(2), D(3)], 3, D(2))
    assert near(got, "1.632993161855452", D("1e-12"))


def test_realised_vol_known_answer_sqrt_5_04() -> None:
    got = ta.realised_vol([D(100), D(110), D(99)], 2)
    assert near(got, "2.244994432064365", D("1e-12"))
    assert ta.realised_vol([D(100), D(110)], 2) is None


def test_updown_volume_ratio_known_answer_and_none_without_down_days() -> None:
    closes = [D(v) for v in (10, 11, 10, 12, 13)]
    vols: list[Decimal | None] = [D(v) for v in (1, 2, 3, 4, 5)]
    assert near(ta.updown_volume_ratio(closes, vols, 4), D(11) / D(3), D("1e-20"))
    assert ta.updown_volume_ratio([D(v) for v in (1, 2, 3, 4, 5)], vols, 4) is None
    assert ta.updown_volume_ratio(closes, [vols[0], None, *vols[2:]], 4) is None


def test_52w_distance_known_answer() -> None:
    bars = [
        Bar(day(0), None, D(10), D(8), D(9), None),
        Bar(day(1), None, D(12), D(9), D(11), None),
        Bar(day(2), None, D(11), D(7), D(10), None),
    ]
    got = ta.high_low_distance_pct(bars, 3)
    assert got is not None
    assert near(got[0], (D(10) / D(12) - 1) * 100, D("1e-20"))
    assert near(got[1], (D(10) / D(7) - 1) * 100, D("1e-20"))
    assert ta.high_low_distance_pct(bars, 4) is None


def test_slope_50dma_known_answer_on_a_ramp() -> None:
    xs = [D(v) for v in range(1, 7)]
    assert near(ta.slope_pct(xs, 3, 2), D(200) / D(3), D("1e-20"))  # SMA3 5 vs 3 two bars ago
    assert ta.slope_pct(xs, 3, 4) is None


def test_weekly_trend_uses_last_close_of_each_iso_week() -> None:
    three_weeks = bars_from_closes(ramp_closes(24, 1))  # 2024-01-01 is a Monday
    assert ta.weekly_closes(three_weeks) == [D(7), D(14), D(21), D(24)]
    bars = bars_from_closes(ramp_closes(300))
    pair = ta.weekly_sma_pair(bars, 10, 40)
    assert pair is not None
    assert near(pair[0], "368.4") and near(pair[1], "263.475")
    assert ta.weekly_sma_pair(bars[:100], 10, 40) is None


def test_relative_strength_ratio_and_change_known_answer() -> None:
    stock = bars_from_closes([D(100), D(110), D(121)])
    bench = bars_from_closes([D(100), D(100), D(110)])
    ratios = ta.relative_ratios(stock, bench)
    assert ratios == [D(1), D("1.1"), D("1.1")]
    assert near(ta.change_pct(ratios, 2), 10)
    assert ta.change_pct(ratios, 3) is None


def test_rs_percentiles_known_answer_and_none_passes_through() -> None:
    got = ta.rs_percentiles({1: D(10), 2: D(20), 3: None, 4: D(30)})
    assert near(got[1], D(100) / 6, D("1e-20"))
    assert got[2] == D(50)
    assert near(got[4], D(500) / 6, D("1e-20"))
    assert got[3] is None


# ---- composition ---------------------------------------------------------------------------


def test_ta_compute_on_linear_ramp_matches_closed_forms_within_1e_6() -> None:
    res = run(ramp_bars(300))
    last = D(399)
    tol = D("1e-6")
    assert near(val(res, "sma_20"), last - D("9.5"), tol)
    assert near(val(res, "sma_50"), last - D("24.5"), tol)
    assert near(val(res, "sma_200"), last - D("99.5"), tol)
    assert near(val(res, "ema_21"), last - 10, tol)
    assert near(val(res, "rsi_14"), 100, tol)
    assert near(val(res, "atr_14"), 2, tol)
    assert near(val(res, "atr_pct"), D(200) / last, tol)
    assert near(val(res, "macd"), 7, tol)
    assert near(val(res, "macd_signal"), 7, tol)
    assert near(val(res, "macd_hist"), 0, tol)
    assert near(val(res, "roc_3m"), last / (last - 63) - 1, tol)
    assert near(val(res, "roc_6m"), last / (last - 126) - 1, tol)
    assert near(val(res, "roc_12m"), last / (last - 252) - 1, tol)
    assert near(val(res, "dist_52w_high_pct"), (last / (last + 1) - 1) * 100, tol)
    assert near(val(res, "dist_52w_low_pct"), (last / (last - 252) - 1) * 100, tol)
    assert near(val(res, "slope_50dma_pct"), D(20) / D("354.5") * 100, tol)
    assert near(val(res, "price_vs_sma200_pct"), (last / (last - D("99.5")) - 1) * 100, tol)
    sd = dmath.sqrt(D("33.25"))
    assert near(val(res, "boll_width_20"), 4 * sd / (last - D("9.5")), tol)
    assert near(val(res, "avg_volume_20"), 1000, tol)
    assert val(res, "weekly_trend") == D(1)
    assert not res.values["updown_volume_ratio_20"].available  # no down session on a ramp


def _float_sma(xs: list[float], n: int) -> float:
    return sum(xs[-n:]) / n


def _float_ema(xs: list[float], n: int) -> list[float]:
    a = 2 / (n + 1)
    out = [sum(xs[:n]) / n]
    for x in xs[n:]:
        out.append(out[-1] + a * (x - out[-1]))
    return out


def _float_rsi(xs: list[float], n: int) -> float:
    ch = [b - a for a, b in zip(xs, xs[1:], strict=False)]
    g = sum(max(c, 0) for c in ch[:n]) / n
    lo = sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        g = (g * (n - 1) + max(c, 0)) / n
        lo = (lo * (n - 1) + max(-c, 0)) / n
    return 100 - 100 / (1 + g / lo)


def _float_atr(bars: list[Bar], n: int) -> float:
    trs = []
    for p, b in zip(bars, bars[1:], strict=False):
        h, lo, pc = float(b.high or 0), float(b.low or 0), float(p.close)
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def test_ta_compute_matches_an_independent_float_reference_on_a_300_bar_series() -> None:
    bars = lcg_bars(300)
    res = run(bars)
    xs = [float(b.close) for b in bars]
    tol = D("1e-6")
    assert near(val(res, "sma_20"), D(_float_sma(xs, 20)), tol)
    assert near(val(res, "sma_200"), D(_float_sma(xs, 200)), tol)
    assert near(val(res, "ema_21"), D(_float_ema(xs, 21)[-1]), tol)
    assert near(val(res, "rsi_14"), D(_float_rsi(xs, 14)), tol)
    assert near(val(res, "atr_14"), D(_float_atr(bars, 14)), tol)
    fast, slow = _float_ema(xs, 12), _float_ema(xs, 26)
    line = [f - s for f, s in zip(fast[14:], slow, strict=True)]
    sig = _float_ema(line, 9)
    assert near(val(res, "macd"), D(line[-1]), tol)
    assert near(val(res, "macd_signal"), D(sig[-1]), tol)
    assert near(val(res, "macd_hist"), D(line[-1] - sig[-1]), tol)


def test_ta_compute_geometric_series_roc_and_zero_volatility() -> None:
    closes = [D(100) * D("1.01") ** i for i in range(300)]
    res = run(bars_from_closes(closes, spread=0))
    tol = D("1e-6")
    assert near(val(res, "roc_3m"), D("1.01") ** 63 - 1, tol)
    assert near(val(res, "roc_12m"), D("1.01") ** 252 - 1, tol)
    assert near(val(res, "vol_20d"), 0, tol)
    assert near(val(res, "vol_60d"), 0, tol)


def test_last_bar_date_and_bars_used_reported() -> None:
    res = run(ramp_bars(30))
    assert res.last_bar_date == day(29)
    assert res.bars_used == 30


def test_fewer_than_200_bars_long_window_indicators_null_with_reason() -> None:
    res = run(ramp_bars(199))
    m = res.values["sma_200"]
    assert not m.available and m.value is None and m.reason == "need 200 bars, have 199"
    for name in ("price_vs_sma200_pct", "roc_12m", "dist_52w_high_pct", "weekly_trend"):
        assert not res.values[name].available
        assert res.values[name].reason
    assert res.values["sma_20"].available and res.values["rsi_14"].available


def test_exactly_200_bars_has_sma200_but_roc_12m_needs_253() -> None:
    res = run(ramp_bars(200))
    assert res.values["sma_200"].available
    assert res.values["roc_6m"].available
    assert res.values["roc_12m"].reason == "need 253 bars, have 200"
    assert res.values["dist_52w_high_pct"].reason == "need 252 bars, have 200"


def test_zero_bars_all_null_without_raising() -> None:
    res = run([])
    assert res.last_bar_date is None and res.bars_used == 0
    assert len(res.values) >= 24
    assert all(not m.available and m.value is None and m.reason for m in res.values.values())


def test_bars_after_as_of_are_ignored() -> None:
    bars = ramp_bars(100)
    cut = run(bars, as_of=day(49))
    assert cut.bars_used == 50 and cut.last_bar_date == day(49)
    assert val(cut, "sma_20") == val(run(bars[:50]), "sma_20")


def test_relative_strength_null_with_reason_without_benchmark() -> None:
    res = run(ramp_bars(200))
    for name in ("rs_benchmark_ratio", "rs_benchmark_change_6m_pct"):
        m = res.values[name]
        assert not m.available and m.reason is not None and "benchmark" in m.reason


def test_relative_strength_vs_sector_null_without_sector_series() -> None:
    res = run(ramp_bars(200), benchmark=ramp_bars(200))
    assert res.values["rs_benchmark_ratio"].available
    for name in ("rs_sector_ratio", "rs_sector_change_6m_pct"):
        m = res.values[name]
        assert not m.available and m.reason is not None and "sector" in m.reason


def test_relative_strength_with_benchmark_known_answer() -> None:
    stock = ramp_bars(200)
    flat = bars_from_closes([D(100)] * 200)
    res = run(stock, benchmark=flat, sector=flat)
    assert near(val(res, "rs_benchmark_ratio"), D("2.99"), D("1e-6"))
    want = (D(299) / D(173) - 1) * 100  # close 299 now, 173 six sessions-windows back
    assert near(val(res, "rs_benchmark_change_6m_pct"), want, D("1e-6"))
    assert near(val(res, "rs_sector_change_6m_pct"), want, D("1e-6"))


def test_misaligned_benchmark_dates_use_the_intersection() -> None:
    stock = ramp_bars(300)
    bench = [b for i, b in enumerate(bars_from_closes([D(100)] * 300)) if i % 2 == 0]
    res = run(stock, benchmark=bench)
    # 150 shared dates (the even days); ratio on the last shared day 298 is 298 / 100 + 0
    assert near(val(res, "rs_benchmark_ratio"), D("3.98"), D("1e-6"))
    assert res.values["rs_benchmark_change_6m_pct"].available  # 150 >= 127 aligned bars
    short = [b for i, b in enumerate(bench) if i < 100]
    cut = run(stock, benchmark=short)
    assert cut.values["rs_benchmark_change_6m_pct"].reason == "need 127 aligned bars, have 100"


def test_missing_volume_makes_volume_metrics_unavailable() -> None:
    res = run(bars_from_closes(ramp_closes(60), volume=None))
    for name in ("avg_volume_20", "updown_volume_ratio_20"):
        assert not res.values[name].available
        assert "volume" in (res.values[name].reason or "")
    assert res.values["sma_20"].available


def test_unsorted_or_duplicate_dates_raise_a_data_error() -> None:
    bars = ramp_bars(10)
    with pytest.raises(BarDataError):
        run(list(reversed(bars)))
    with pytest.raises(BarDataError):
        run(bars + [bars[-1]])


def test_every_value_is_decimal_or_none_and_output_is_deterministic() -> None:
    bars = lcg_bars(260)
    a, b = run(bars), run(bars)
    assert a == b
    for m in a.values.values():
        assert m.value is None or isinstance(m.value, Decimal)
        assert m.available == (m.value is not None)
        assert (m.reason is None) == m.available


def test_only_restricts_the_computed_metrics_and_rejects_unknown_names() -> None:
    res = run(ramp_bars(60), only={"sma_20", "rsi_14"})
    assert set(res.values) == {"sma_20", "rsi_14"}
    with pytest.raises(ValueError):
        run(ramp_bars(60), only={"sma_21"})


def test_config_windows_drive_the_metric_names() -> None:
    cfg = TaSettings(sma_windows=[5], rsi_period=7, atr_period=7)
    res = run(ramp_bars(60), cfg=cfg)
    assert {"sma_5", "rsi_7", "atr_7"} <= set(res.values)
    assert "sma_20" not in res.values
