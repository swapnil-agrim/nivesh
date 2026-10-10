from datetime import date, timedelta
from decimal import Decimal

import pytest

from nivesh_core.analysis_config import MarketCapBands, XraySettings
from nivesh_core.errors import NiveshError
from nivesh_core.universe_config import UniverseSettings
from nivesh_engine import universe as U
from nivesh_engine.bars import Bar
from nivesh_engine.universe import (
    Candidate,
    adv_value,
    build_universe,
    liquidity_ok,
    market_cap_bucket,
    matches_exclusion,
)

D = Decimal
CFG = UniverseSettings()
ASOF = date(2026, 10, 9)


def bars(n: int, close: str, volume: int | None, *, end: date = ASOF) -> list[Bar]:
    return [
        Bar(end - timedelta(days=n - 1 - i), None, None, None, D(close),
            None if volume is None else D(volume))
        for i in range(n)
    ]  # fmt: skip


# ---- ADV ---------------------------------------------------------------------------------------
def test_adv_value_is_mean_volume_times_last_close_times_fx() -> None:
    b = bars(20, "10", 100)
    b[-1] = Bar(b[-1].date, None, None, None, D("12"), D(300))  # mean volume (19*100+300)/20 = 110
    assert adv_value(b, 20).value == D(110) * D(12)
    assert adv_value(b, 20, D("90")).value == D(110) * D(12) * D(90)


def test_adv_value_none_when_fewer_than_window_volume_bars() -> None:
    r = adv_value(bars(19, "10", 100), 20)
    assert r.value is None and r.reason == "need 20 bars with volume, have 19"
    b = bars(20, "10", 100)
    b[3] = Bar(b[3].date, None, None, None, D(10), None)
    r = adv_value(b, 20)
    assert r.value is None and r.reason == "volume missing in the last 20 bars"


def test_adv_value_ignores_bars_after_as_of() -> None:
    b = bars(25, "10", 100)
    assert adv_value(b, 20, as_of=b[-6].date).last_bar == b[-6].date
    assert adv_value(b, 20, as_of=b[0].date).value is None


def test_inr_floor_exactly_5_crore_passes_and_one_paisa_below_fails() -> None:
    assert liquidity_ok("IN", bars(20, "500", 100000), ASOF, 20, CFG).ok  # 5e7 exactly
    below = liquidity_ok("IN", bars(20, "499.9999999", 100000), ASOF, 20, CFG)
    assert not below.ok and below.reason == "below the liquidity floor"
    assert below.adv == D("49999999.99")


def test_usd_floor_exactly_20_million_passes_and_just_below_fails() -> None:
    assert liquidity_ok("US", bars(20, "20", 1000000), ASOF, 20, CFG).ok
    assert not liquidity_ok("US", bars(20, "19.99", 1000000), ASOF, 20, CFG).ok


def test_usd_floor_compares_before_fx() -> None:
    # 19M USD is above 5 crore INR in rupee terms but below the 20M USD floor: no FX is applied
    r = liquidity_ok("US", bars(20, "19", 1000000), ASOF, 20, CFG)
    assert not r.ok and r.adv == D(19000000)


def test_stale_last_bar_fails_with_insufficient_liquidity_data() -> None:
    old = bars(20, "500", 100000, end=ASOF - timedelta(days=8))
    r = liquidity_ok("IN", old, ASOF, 20, CFG)
    assert not r.ok and r.reason.startswith("insufficient liquidity data")
    assert liquidity_ok(
        "IN", bars(20, "500", 100000, end=ASOF - timedelta(days=7)), ASOF, 20, CFG
    ).ok


def test_missing_volume_fails_never_guessed() -> None:
    r = liquidity_ok("IN", bars(20, "500", None), ASOF, 20, CFG)
    assert not r.ok and r.reason.startswith("insufficient liquidity data") and r.adv is None
    assert liquidity_ok("IN", [], ASOF, 20, CFG).reason.startswith("insufficient liquidity data")
    assert liquidity_ok("IN", bars(5, "500", 100000), ASOF, 20, CFG).adv is None


def test_unknown_market_is_refused() -> None:
    with pytest.raises(NiveshError, match="no liquidity floor"):
        liquidity_ok("JP", bars(20, "1", 1), ASOF, 20, CFG)


def test_results_are_exact_decimals() -> None:
    r = liquidity_ok("US", bars(20, "20.10", 1000000), ASOF, 20, CFG)
    assert r.adv == D("20100000.00") and isinstance(r.adv, Decimal)


# ---- exclusions and cap bucket -----------------------------------------------------------------
def test_exclusion_by_symbol_isin_name_still_works() -> None:
    for ex in ("aaa", "INE000A01010", "  Alpha Limited "):
        assert matches_exclusion([ex], "AAA", "INE000A01010", "Alpha Limited", "Energy")
    assert not matches_exclusion(["BBB"], "AAA", "INE000A01010", "Alpha Limited", "Energy")
    assert not matches_exclusion([], "AAA", None, None, None)


def test_exclusion_by_sector_casefolded() -> None:
    assert matches_exclusion(["TOBACCO"], "AAA", None, "Alpha", "tobacco")
    assert not matches_exclusion(["TOBACCO"], "AAA", None, "Alpha", "Tobacco products")  # exact
    assert not matches_exclusion(["TOBACCO"], "AAA", None, "Alpha", None)


def cand(i: int, *, sector: str | None = "Tech", market: str = "IN", n: int = 20,
         volume: int | None = 100000, close: str = "500",
         member_of: tuple[str, ...] = ("NIFTY500",)) -> Candidate:  # fmt: skip
    return Candidate(i, f"S{i}", None, f"Name {i}", sector, market, bars(n, close, volume),
                     frozenset(member_of))  # fmt: skip


def test_exclusion_by_sector_casefolded_removes_member_with_reason() -> None:
    res = build_universe([cand(1, sector="Tobacco"), cand(2)], ["tobacco"], ASOF, 20, CFG)
    assert res.ids == (2,)
    assert [(r.security_id, r.reason) for r in res.removed] == [(1, "excluded by profile")]


def test_build_universe_unions_indices_dedupes_and_orders_by_security_id() -> None:
    cs = [cand(5), cand(2, member_of=("SP500", "NASDAQ100")), cand(2), cand(9)]
    res = build_universe(cs, [], ASOF, 20, CFG)
    assert res.ids == (2, 5, 9) and res.considered == 3
    assert res.member_of[2] == frozenset({"SP500", "NASDAQ100", "NIFTY500"})


def test_each_removed_name_carries_one_reason() -> None:
    cs = [
        cand(1),
        cand(2, sector="Tobacco"),
        cand(3, close="1"),
        cand(4, n=5),
        cand(5, volume=None),
    ]
    res = build_universe(cs, ["Tobacco"], ASOF, 20, CFG)
    why = {r.security_id: r.reason for r in res.removed}
    assert res.ids == (1,)
    assert why[2] == "excluded by profile" and why[3] == "below the liquidity floor"
    assert why[4].startswith("insufficient liquidity data") and why[5].startswith("insufficient")
    assert len(res.removed) == len(why) == 4
    assert res.counts() == {"excluded by profile": 1, "below the liquidity floor": 1,
                            "insufficient liquidity data": 2}  # fmt: skip


def test_empty_after_filters_raises_with_actionable_message() -> None:
    with pytest.raises(NiveshError, match="empty after the exclusions and the liquidity floor"):
        build_universe([cand(1, close="1"), cand(2, close="1")], [], ASOF, 20, CFG)
    with pytest.raises(NiveshError, match="no candidates"):
        build_universe([], [], ASOF, 20, CFG)


def test_market_cap_bucket_uses_xray_bands_and_unclassified_when_unset() -> None:
    cfg = XraySettings(market_cap={"IN": MarketCapBands(large_min=D(1000), mid_min=D(100))})
    assert market_cap_bucket(D(1000), "IN", cfg) == "large"
    assert market_cap_bucket(D(100), "IN", cfg) == "mid"
    assert market_cap_bucket(D(99), "IN", cfg) == "small"
    assert market_cap_bucket(None, "IN", cfg) == "unclassified"
    assert market_cap_bucket(D(5), "US", cfg) == "unclassified"
    assert market_cap_bucket(D(5), "IN", XraySettings()) == "unclassified"


def test_module_has_no_unclassified_literal_drift() -> None:
    assert U.UNCLASSIFIED == "unclassified"
