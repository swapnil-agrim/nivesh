from decimal import Decimal

from nivesh_core.analysis_config import LevelsSettings
from nivesh_engine import levels
from nivesh_engine.levels import Level
from tests.analysis_fx import bars_from_closes, day, hl_bars, ramp_closes

D = Decimal

# highs and lows of a short series with three swing highs and two swing lows (window 2)
HIGHS = [101, 102, 105, 102, 101, 103, 106, 103, 102, 101, 104, 102, 101]
LOWS = [100, 101, 104, 101, 100, 102, 105, 102, 101, 100, 103, 101, 100]


def test_swing_pivot_high_and_low_known_series() -> None:
    p = levels.swing_pivots(hl_bars(HIGHS, LOWS), 2)
    assert [(x.index, x.price) for x in p.highs] == [(2, D(105)), (6, D(106)), (10, D(104))]
    assert [(x.index, x.price) for x in p.lows] == [(4, D(100)), (9, D(100))]
    assert p.highs[0].date == day(2)


def test_pivot_ties_take_the_first_bar() -> None:
    highs = [100, 100, 103, 103, 100, 100, 100]
    p = levels.swing_pivots(hl_bars(highs, [h - 1 for h in highs]), 2)
    assert [x.index for x in p.highs] == [2]


def test_last_w_bars_cannot_be_confirmed_pivots() -> None:
    highs = [100, 100, 100, 100, 109]
    p = levels.swing_pivots(hl_bars(highs, [h - 1 for h in highs]), 2)
    assert p.highs == [] and levels.swing_pivots(hl_bars(highs[:3], highs[:3]), 2).highs == []


def test_cluster_merges_pivots_within_tolerance_with_mean_level_and_touch_count() -> None:
    prices = [D(v) for v in (150, 111, 100, 110, 101)]
    got = levels.cluster_prices(prices, D("1.5"))
    assert got == [Level(D("100.5"), 2), Level(D("110.5"), 2), Level(D(150), 1)]
    assert levels.cluster_prices([], D("1.5")) == []
    assert levels.cluster_prices([D(0), D(0)], D("1.5")) == [Level(D(0), 2)]


def test_at_most_three_levels_per_side_ranked_by_touches_then_proximity_then_price() -> None:
    pool = [
        Level(D(90), 1), Level(D(95), 3), Level(D(97), 3), Level(D(80), 5), Level(D(70), 1),
        Level(D(120), 2), Level(D(110), 2), Level(D(130), 4), Level(D(105), 1), Level(D(140), 1),
    ]  # fmt: skip
    support, resistance = levels.rank_levels(pool, D(100), 3)
    assert [x.price for x in support] == [D(80), D(97), D(95)]
    assert [x.price for x in resistance] == [D(130), D(110), D(120)]


def test_levels_split_around_the_last_close() -> None:
    cfg = LevelsSettings(pivot_window=2)
    got = levels.find_levels(hl_bars(HIGHS, LOWS), cfg)
    assert got.reason is None
    assert got.support == [Level(D(100), 2)]  # last close is 100.5
    assert got.resistance == [Level(D(105), 3)]
    only = levels.cluster_levels(hl_bars(HIGHS, LOWS), cfg)
    assert only == [Level(D(100), 2), Level(D(105), 3)]


def test_no_pivots_returns_empty_with_reason() -> None:
    got = levels.find_levels(bars_from_closes(ramp_closes(40)), LevelsSettings())
    assert got.support == [] and got.resistance == [] and got.gaps == []
    assert got.reason == "no confirmed swing pivots"
    assert levels.find_levels([], LevelsSettings()).reason == "no confirmed swing pivots"


def test_up_and_down_gap_zones_unfilled_only() -> None:
    highs = [100, 150, 160, 170, 120, 125, 126]
    lows = [80, 120, 130, 140, 110, 115, 116]
    got = levels.gap_zones(hl_bars(highs, lows), LevelsSettings())
    assert [(g.direction, g.low, g.high, g.date) for g in got] == [
        ("down", D(120), D(140), day(4)),
        ("up", D(100), D(120), day(1)),
    ]


def test_filled_gap_is_dropped() -> None:
    highs = [100, 150, 160, 170, 120, 125, 145]
    lows = [80, 120, 130, 140, 110, 115, 116]
    got = levels.gap_zones(hl_bars(highs, lows), LevelsSettings())
    assert [g.direction for g in got] == ["up"]  # the down gap was refilled on the last bar
    refilled_up = levels.gap_zones(hl_bars([100, 150, 160], [80, 120, 99]), LevelsSettings())
    assert refilled_up == []


def test_small_gaps_and_the_zone_cap_follow_config() -> None:
    tiny = levels.gap_zones(hl_bars([100, 102], [90, 100]), LevelsSettings())
    assert tiny == []  # 0 gap: low equals the previous high
    small = levels.gap_zones(hl_bars([1000, 1030], [900, 1005]), LevelsSettings())
    assert small == []  # 0.5 percent is below the one percent minimum
    highs, lows = [100, 130, 131, 160, 161, 190, 191], [90, 110, 120, 140, 150, 170, 180]
    capped = levels.gap_zones(hl_bars(highs, lows), LevelsSettings(max_gap_zones=2))
    assert len(capped) == 2 and capped[0].date > capped[1].date


def test_levels_are_deterministic_on_ties() -> None:
    cfg = LevelsSettings(pivot_window=2)
    bars = hl_bars(HIGHS * 3, LOWS * 3)
    assert levels.find_levels(bars, cfg) == levels.find_levels(bars, cfg)
