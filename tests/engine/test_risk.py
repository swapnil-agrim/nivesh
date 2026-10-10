from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import RiskSettings
from nivesh_engine import dmath
from nivesh_engine.risk import RiskCandidate, RiskHolding, risk_metrics
from nivesh_engine.ta import realised_vol
from tests.analysis_fx import bars_from_closes, lcg_bars, legs_closes

D = Decimal
CFG = RiskSettings(min_overlap_days=20)


def closes_from_returns(rets: list[Decimal], start: int = 100) -> list[Decimal]:
    out = [D(start)]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out


def near(a: Decimal | None, b: Decimal | str, tol: str = "1e-7") -> bool:
    return a is not None and abs(a - D(b)) <= D(tol)


def pair(a: int, b: int, value: int = 500) -> list[RiskHolding]:
    return [RiskHolding(a, "A", D(value)), RiskHolding(b, "B", D(value))]


def run(holdings, bars, *, candidate=None, benchmarks=None, cfg=CFG, **kw):  # type: ignore[no-untyped-def]
    return risk_metrics(holdings, candidate, bars, benchmarks or {}, cfg=cfg, **kw)


PSEUDO = lcg_bars(61)
PSEUDO_CLOSES = [b.close for b in PSEUDO]
PSEUDO_RETS = [b / a - 1 for a, b in zip(PSEUDO_CLOSES, PSEUDO_CLOSES[1:], strict=False)]
LOOK60 = RiskSettings(lookback_days=60, min_overlap_days=20)


def test_portfolio_vol_two_perfectly_correlated_equal_vol_assets_equals_asset_vol() -> None:
    res = run(pair(1, 2), {1: PSEUDO, 2: PSEUDO}, cfg=LOOK60)
    alone = realised_vol(PSEUDO_CLOSES, 60)
    assert alone is not None and near(res.portfolio_vol.value, alone)


def test_portfolio_vol_uncorrelated_equal_weights_is_vol_over_root_two() -> None:
    a = D("0.01")
    r1 = [a, a, -a, -a] * 15
    r2 = [a, -a, a, -a] * 15
    c1, c2 = closes_from_returns(r1), closes_from_returns(r2)
    res = run(pair(1, 2), {1: bars_from_closes(c1), 2: bars_from_closes(c2)}, cfg=LOOK60)
    one = realised_vol(c1, 60)
    assert one is not None and near(res.portfolio_vol.value, one / dmath.sqrt(D(2)))


def drawdown_path() -> list[Decimal]:
    return legs_closes(
        100,
        [(100, "1"), (100, "-1"), (100, "1"), (100, "1"), (107, "0"), (60, "-1"), (192, "0")],
    )


def test_max_drawdown_1y_and_3y_known_series() -> None:
    path = drawdown_path()
    assert len(path) == 760
    res = run([RiskHolding(1, "A", D(100))], {1: bars_from_closes(path)})
    got = res.max_drawdown_pct
    assert near(got["1y"].value, 20) and near(got["3y"].value, 50)  # 300 to 240; 200 to 100


def test_drawdown_window_insufficient_bars_reason() -> None:
    bars = bars_from_closes(drawdown_path()[:100])
    got = run([RiskHolding(1, "A", D(100))], {1: bars}).max_drawdown_pct
    assert not got["1y"].available and got["1y"].reason == "need 253 aligned bars, have 100"
    assert got["3y"].reason == "need 757 aligned bars, have 100"


def test_beta_two_times_benchmark_series_is_two() -> None:
    b = [D("0.01"), D("-0.01")] * 30
    bench = bars_from_closes(closes_from_returns(b))
    stock = bars_from_closes(closes_from_returns([2 * r for r in b]))
    res = run([RiskHolding(1, "A", D(100))], {1: stock}, benchmarks={"IN": bench}, cfg=LOOK60)
    assert near(res.holdings[0].beta.value, 2) and near(res.beta.value, 2)


def test_beta_unavailable_without_benchmark_with_reason() -> None:
    res = run([RiskHolding(1, "A", D(100))], {1: PSEUDO}, cfg=LOOK60)
    assert not res.holdings[0].beta.available and "benchmark" in (res.holdings[0].beta.reason or "")
    assert not res.beta.available and "benchmark" in (res.beta.reason or "")


def candidate(weight: str = "10", **kw: object) -> RiskCandidate:
    return RiskCandidate(9, "Cand", D(weight), **kw)  # type: ignore[arg-type]


def test_candidate_correlation_plus_one_and_minus_one_known_answers() -> None:
    inverse = bars_from_closes(closes_from_returns([-r for r in PSEUDO_RETS]))
    same = run([RiskHolding(1, "A", D(100))], {1: PSEUDO, 9: PSEUDO}, candidate=candidate())
    opposite = run([RiskHolding(1, "A", D(100))], {1: PSEUDO, 9: inverse}, candidate=candidate())
    assert near(same.holdings[0].correlation.value, 1)
    assert near(opposite.holdings[0].correlation.value, -1)


def test_correlation_unavailable_when_overlap_below_min_overlap_days() -> None:
    res = run([RiskHolding(1, "A", D(100))], {1: PSEUDO, 9: PSEUDO[:10]}, candidate=candidate())
    c = res.holdings[0].correlation
    assert not c.available and "overlap" in (c.reason or "")
    assert not run([RiskHolding(1, "A", D(100))], {1: PSEUDO}).holdings[0].correlation.available


def flat(price: int, volume: int | None, n: int = 30):  # type: ignore[no-untyped-def]
    return bars_from_closes([D(price)] * n, volume=volume)


def test_days_to_trade_known_answer_4_days() -> None:
    # 20-day average volume 100000 at 50: 5,000,000 a day, 10 percent is 500,000 a day
    held = [RiskHolding(1, "Big", D(8_000_000)), RiskHolding(2, "Small", D(2_000_000))]
    bars = {1: flat(50, 100000), 2: flat(50, 100000), 9: flat(50, 100000)}
    res = run(held, bars, candidate=candidate("20"))  # 20 percent of 10,000,000 = 2,000,000
    by = {h.security_id: h.days_to_trade for h in res.holdings}
    assert by[2].value == D(4) and by[1].value == D(16)
    assert res.candidate_days_to_trade is not None and res.candidate_days_to_trade.value == D(4)
    usd = [RiskHolding(1, "Us", D(2_000_000), "US", None, D(80))]
    got = run(usd, {1: flat(50, 100000)}).holdings[0].days_to_trade
    assert got.value is not None and got.value == D(2_000_000) / (D("0.1") * 5_000_000 * 80)


def test_days_to_trade_unavailable_without_volume() -> None:
    res = run([RiskHolding(1, "A", D(100))], {1: flat(50, None)}, candidate=candidate())
    assert not res.holdings[0].days_to_trade.available
    assert "volume" in (res.holdings[0].days_to_trade.reason or "")
    short = run([RiskHolding(1, "A", D(100))], {1: flat(50, 1000, 5)})
    assert "need 20" in (short.holdings[0].days_to_trade.reason or "")


def test_pro_forma_weights_fund_the_candidate_pro_rata_and_sum_to_100() -> None:
    held = [RiskHolding(1, "A", D(600)), RiskHolding(2, "B", D(400))]
    res = run(held, {}, candidate=candidate("20"))
    pf = res.pro_forma
    assert pf is not None and pf.candidate_weight_pct == D(20)
    weights = {w.name: w.weight_pct for w in pf.positions}
    assert weights == {"A": D(48), "B": D(32), "Cand": D(20)}
    assert sum(weights.values(), D(0)) == D(100)
    assert near(pf.hhi, D("0.48") ** 2 + D("0.32") ** 2 + D("0.2") ** 2)
    assert res.holdings[0].weight_pct == D(60)  # current weights, before the candidate


def test_pro_forma_sector_weight_adds_candidate_sector_and_flags_limit_breach() -> None:
    held = [
        RiskHolding(1, "A", D(500), sector="IT"),
        RiskHolding(2, "B", D(500), sector="Banks"),
    ]
    res = run(
        held, {}, candidate=candidate("20", sector="IT"),
        max_position_pct=D(45), max_sector_pct=D(50),
    )  # fmt: skip
    pf = res.pro_forma
    assert pf is not None
    assert {w.name: w.weight_pct for w in pf.sectors} == {"IT": D(60), "Banks": D(40)}
    assert [w.name for w in pf.sectors_over_limit] == ["IT"]
    assert pf.positions_over_limit == ()  # 40, 40, 20 against 45
    tight = run(held, {}, candidate=candidate("20"), max_position_pct=D(35))
    assert tight.pro_forma is not None
    assert [w.name for w in tight.pro_forma.positions_over_limit] == ["A", "B"]
    assert {w.name for w in tight.pro_forma.sectors} == {"IT", "Banks", "unclassified"}
    assert run(held, {}).pro_forma is None


def test_holdings_without_bars_are_excluded_from_vol_and_reported_with_coverage_pct() -> None:
    held = [RiskHolding(1, "A", D(600)), RiskHolding(2, "B", D(400))]
    res = run(held, {1: PSEUDO}, cfg=LOOK60)
    alone = realised_vol(PSEUDO_CLOSES, 60)
    assert near(res.portfolio_vol.value, alone or D(0)) and res.coverage_pct == D(60)
    assert [(e.security_id, "bars" in e.reason) for e in res.excluded] == [(2, True)]
    nothing = run(held, {})
    assert not nothing.portfolio_vol.available and nothing.coverage_pct == D(0)
    empty = run([], {})
    assert empty.total_inr == D(0) and not empty.portfolio_vol.available


def test_overlap_shorter_than_the_minimum_leaves_volatility_unavailable() -> None:
    later = bars_from_closes(PSEUDO_CLOSES[:30], start=50)  # shares only 11 days with PSEUDO
    res = run(pair(1, 2), {1: PSEUDO, 2: later}, cfg=LOOK60)
    assert not res.portfolio_vol.available
    assert "overlap" in (res.portfolio_vol.reason or "")


def test_bars_after_as_of_are_ignored() -> None:
    cut = PSEUDO[40].date
    full = run(
        [RiskHolding(1, "A", D(100))],
        {1: PSEUDO},
        cfg=RiskSettings(lookback_days=40, min_overlap_days=20),
        as_of=cut,
    )
    trimmed = run(
        [RiskHolding(1, "A", D(100))], {1: PSEUDO[:41]},
        cfg=RiskSettings(lookback_days=40, min_overlap_days=20),
    )  # fmt: skip
    assert full.portfolio_vol == trimmed.portfolio_vol and full.as_of == cut == date(2024, 2, 10)


def test_risk_metrics_are_deterministic() -> None:
    held = [RiskHolding(1, "A", D(600), sector="IT"), RiskHolding(2, "B", D(400))]
    bars = {1: PSEUDO, 2: lcg_bars(61, seed=11), 9: lcg_bars(61, seed=5)}
    bench = {"IN": lcg_bars(61, seed=3)}
    a = run(held, bars, candidate=candidate(), benchmarks=bench, cfg=LOOK60)
    b = run(list(reversed(held)), bars, candidate=candidate(), benchmarks=bench, cfg=LOOK60)
    assert a == b
