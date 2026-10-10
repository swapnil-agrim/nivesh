import time
from datetime import date
from decimal import Decimal

import pytest
import yaml

from nivesh_core.analysis_config import AnalysisSettings, ScreenSettings
from nivesh_core.errors import ConfigError
from nivesh_core.market_store import EstimateRow
from nivesh_engine import metrics
from nivesh_engine.metrics import REGISTRY, SecurityInputs, inputs_needed
from nivesh_engine.screen import RuleSet, ScreenResult, bundles_needed, parse_rules, screen
from tests.analysis_fx import (
    UNIVERSE_ASOF,
    annual_rows,
    bars_from_closes,
    ramp_closes,
    seed_universe,
)

D = Decimal
CFG = AnalysisSettings()
ASOF = date(2024, 6, 30)

BOOK: dict[str, list[str | int]] = {
    "revenue": [1000],
    "gross_profit": [400],
    "operating_income": [150],
    "depreciation_amortization": [50],
    "net_income": [100],
    "total_equity": [450, 550],
    "total_debt": [200],
    "cash": [150],
    "interest_expense": [25],
    "current_assets": [300],
    "current_liabilities": [200],
    "total_assets": [1000],
    "cfo": [130],
    "capex": [40],
}  # roce 150 / (1000 - 200) = 18.75, net margin 10


def inp(sid: int, *, book: dict | None = None, market: str = "US", **kw) -> SecurityInputs:  # type: ignore[type-arg]
    rows = tuple(annual_rows(BOOK if book is None else book))
    return SecurityInputs(sid, f"T{sid}", market, None, "general", rows=rows, **kw)


def ruleset(*rules: dict) -> RuleSet:  # type: ignore[type-arg]
    return parse_rules(yaml.safe_dump({"rules": list(rules)}))


def rule(rid: str, metric: str, op: str, value: object, **kw: object) -> dict:  # type: ignore[type-arg]
    return {"id": rid, "metric": metric, "op": op, "value": value, **kw}


def run(
    universe: dict, rules: RuleSet, cfg: AnalysisSettings = CFG, as_of: date = ASOF
) -> ScreenResult:  # type: ignore[type-arg]
    return screen(universe, rules, as_of=as_of, cfg=cfg)


def matched(res: ScreenResult) -> list[int]:
    return [m.security_id for m in res.matches]


# ---- the rule file ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "rules: [{id: a, metric: roce, op: '=>', value: 1}]",  # bad operator
        "rules: [{id: a, metric: not_a_metric, op: '>', value: 1}]",  # unknown metric
        "rules: [{id: a, metric: roce, op: '>', value: 1, flavour: x}]",  # extra rule key
        "rules: [{id: a, metric: roce, op: '>', value: 1}]\nextra: 1",  # extra top-level key
        "rules: [{id: a, metric: roce, op: '>'}]",  # no value
        "rules: [{id: a, metric: roce, op: between, value: 5}]",  # between needs two
        "rules: [{id: a, metric: roce, op: between, value: [9, 3]}]",  # unsorted bounds
        "rules: [{id: a, metric: roce, op: '>', value: [1, 2]}]",  # list for a plain operator
        "rules: [{id: a, metric: setup_type, op: '>', value: x}]",  # string metric ordering
        "rules: [{id: a, metric: roce, op: '>', value: high}]",  # text for a numeric metric
        "rules: [{id: a, metric: roce, op: '>', value: true}]",  # a flag is not a number
        "rules: [{id: a, metric: roce, op: '>', value: 1},"
        " {id: a, metric: roe, op: '>', value: 2}]",  # duplicate id
        "rules: []",
        "- not a mapping",
        "rules: [unclosed",
        "rules: [{id: '', metric: roce, op: '>', value: 1}]",
    ],
)
def test_rule_file_schema_rejects_bad_op_unknown_metric_and_extra_keys(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_rules(text)


def test_rule_file_rejects_more_rules_than_the_budget() -> None:
    text = yaml.safe_dump({"rules": [rule(f"r{i}", "roce", ">", 1) for i in range(3)]})
    with pytest.raises(ConfigError, match="at most 2"):
        parse_rules(text, max_rules=2)
    assert len(parse_rules(text, max_rules=3).rules) == 3


def test_rule_file_accepts_numbers_text_ranges_and_a_name() -> None:
    rs = parse_rules(
        yaml.safe_dump(
            {
                "name": "mine",
                "rules": [
                    rule("a", "roce", ">", 15.5),
                    rule("b", "roce", "between", [10, 20], required=True),
                    rule("c", "setup_type", "==", "pullback"),
                ],
            }
        )
    )
    assert rs.name == "mine" and [r.id for r in rs.rules] == ["a", "b", "c"]
    assert rs.rules[0].value == D("15.5") and rs.rules[1].required and not rs.rules[0].required
    assert rs.rules[2].value == "pullback"


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        (">", 18, True), (">", 18.75, False), (">=", 18.75, True), (">=", 19, False),
        ("<", 19, True), ("<", 18.75, False), ("<=", 18.75, True), ("<=", 18, False),
        ("==", 18.75, True), ("==", 18, False), ("!=", 18, True), ("!=", 18.75, False),
        ("between", [18, 19], True), ("between", [19, 20], False), ("between", [18.75, 20], True),
        ("between", [10, 18.75], True),
    ],
)  # fmt: skip
def test_each_operator_passes_and_fails_correctly(op: str, value: object, expected: bool) -> None:
    res = run({1: inp(1)}, ruleset(rule("r", "roce", op, value)))
    assert (matched(res) == [1]) is expected


def test_matches_carry_the_values_that_passed_per_rule() -> None:
    rs = ruleset(rule("quality", "roce", ">", 10), rule("margin", "net_margin_pct", ">", 5))
    (m,) = run({1: inp(1)}, rs).matches
    assert [(v.rule_id, v.metric, v.value) for v in m.values] == [
        ("quality", "roce", D("18.75")),
        ("margin", "net_margin_pct", D("10")),
    ]
    assert m.symbol == "T1" and not m.partial and m.skipped_rules == ()


# ---- unavailable data ------------------------------------------------------------------------
def test_rule_unavailable_for_the_whole_universe_is_skipped_and_reported_with_reason() -> None:
    rs = ruleset(rule("quality", "roce", ">", 10), rule("pledge", "promoter_pledged_pct", "<", 20))
    res = run({1: inp(1), 2: inp(2)}, rs)
    assert matched(res) == [1, 2]
    (skip,) = res.skipped
    assert (skip.rule_id, skip.unavailable, skip.evaluated) == ("pledge", 2, 2)
    assert "India" in skip.reason
    assert all(m.skipped_rules == ("pledge",) and m.partial for m in res.matches)


def test_rule_unavailable_for_one_security_marks_the_match_partial_and_lists_the_skipped_rule() -> (
    None
):
    thin = {k: v for k, v in BOOK.items() if k != "current_liabilities"}
    rs = ruleset(rule("quality", "roce", ">", 10), rule("margin", "net_margin_pct", ">", 5))
    res = run({1: inp(1), 2: inp(2, book=thin)}, rs)
    full, part = res.matches
    assert (full.partial, full.skipped_rules) == (False, ())
    assert (part.partial, part.skipped_rules) == (True, ("quality",))
    assert [v.rule_id for v in part.values] == ["margin"]  # only what was evaluated and passed
    assert res.skipped == ()  # the rule did run for security 1


def test_required_rule_with_unavailable_data_fails_the_security_with_reason() -> None:
    thin = {k: v for k, v in BOOK.items() if k != "current_liabilities"}
    rs = ruleset(
        rule("quality", "roce", ">", 10, required=True), rule("m", "net_margin_pct", ">", 5)
    )
    res = run({1: inp(1), 2: inp(2, book=thin)}, rs)
    assert matched(res) == [1]
    (rej,) = res.rejected
    assert (rej.security_id, rej.rule_id) == (2, "quality") and rej.reason


def test_security_with_every_rule_skipped_is_not_a_match() -> None:
    thin = {k: v for k, v in BOOK.items() if k != "current_liabilities"}
    res = run({1: inp(1), 2: inp(2, book=thin)}, ruleset(rule("quality", "roce", ">", 10)))
    assert matched(res) == [1]


def test_revisions_unavailable_for_india_is_skipped_never_zero() -> None:
    india = inp(1, market="IN", book={"revenue": [1000], "net_income": [100]})
    for op, value in ((">=", 0), ("==", 0)):
        res = run({1: india}, ruleset(rule("rev", "revisions", op, value)))
        assert res.matches == () and "India" in res.skipped[0].reason


def test_revisions_is_the_eps_estimate_change_over_the_lookback_for_a_us_security() -> None:
    est = (
        EstimateRow(1, "eps", "2025-12-31", D("5.0"), date(2024, 4, 1), "fmp"),
        EstimateRow(1, "eps", "2025-12-31", D("5.5"), ASOF, "fmp"),
        EstimateRow(1, "eps", "2025-12-31", D("1.0"), date(2023, 1, 1), "fmp"),  # before the window
    )
    (m,) = run({1: inp(1, estimates=est)}, ruleset(rule("rev", "revisions", ">=", 0))).matches
    assert m.values[0].value == D(10)
    one = (EstimateRow(1, "eps", "2025-12-31", D("5.5"), ASOF, "fmp"),)
    res = run({1: inp(1, estimates=one)}, ruleset(rule("rev", "revisions", ">=", 0)))
    assert res.matches == () and "snapshot" in res.skipped[0].reason


# ---- the registry and laziness ---------------------------------------------------------------
EXPECTED_METRICS = {
    # ST-6.1 technicals
    "sma_20", "sma_50", "sma_200", "ema_21", "slope_50dma_pct", "price_vs_sma200_pct", "rsi_14",
    "macd_hist", "roc_3m", "roc_6m", "roc_12m", "dist_52w_high_pct", "dist_52w_low_pct", "atr_pct",
    "vol_20d", "vol_60d", "avg_volume_20", "updown_volume_ratio_20",
    "rs_benchmark_change_6m_pct", "rs_sector_change_6m_pct",
    # ST-6.3 fundamentals
    "roce", "roe", "roic", "gross_margin_pct", "operating_margin_pct", "ebitda_margin_pct",
    "net_margin_pct", "debt_equity", "net_debt_ebitda", "interest_cover", "current_ratio",
    "cfo_to_pat", "fcf_margin_pct", "revenue_cagr", "net_income_cagr", "eps_cagr",
    "promoter_pledged_pct", "promoter_change_4q_pp", "gnpa_pct", "nnpa_pct", "nim_pct", "casa_pct",
    "car_pct",
    # ST-6.4 valuation
    "pe", "pb", "ev_ebitda", "fcf_yield_pct", "dividend_yield_pct", "valuation_percentile",
    "pe_vs_peer_median_pct",
    # estimates, flags, setups and the universe (ST-6.8, ST-9.2)
    "revisions", "hard_flag_count", "soft_flag_count", "setup_type", "base_breakout",
    "pullback_to_50dma", "rs_percentile",
}  # fmt: skip


def test_registry_covers_st_6_8_and_st_9_2_metric_names() -> None:
    assert set(REGISTRY) == EXPECTED_METRICS
    assert {"roce", "net_debt_ebitda", "valuation_percentile", "revisions"} <= set(REGISTRY)
    assert {"price_vs_sma200_pct", "rs_percentile", "base_breakout", "pullback_to_50dma"} <= set(
        REGISTRY
    )
    assert REGISTRY["setup_type"].kind == "string" and REGISTRY["roce"].kind == "numeric"
    assert all(d.name == k for k, d in REGISTRY.items())


def test_inputs_needed_follows_the_rules_metrics() -> None:
    assert inputs_needed(["roce"]) == {"statements"}
    assert {"bars", "benchmark"} <= inputs_needed(["rs_percentile"])
    assert {"statements", "closes"} <= inputs_needed(["valuation_percentile"])
    assert "peers" in inputs_needed(["pe_vs_peer_median_pct"])
    assert "estimates" in inputs_needed(["revisions"])
    assert bundles_needed(ruleset(rule("a", "roce", ">", 1), rule("b", "sma_20", ">", 1))) == {
        "fa",
        "ta",
    }


class Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: dict[str, int] = {}
        for name in (
            "fa_compute",
            "ta_compute",
            "valuation_multiples",
            "detect_flags",
            "classify_setup",
        ):
            self._wrap(monkeypatch, name)

    def _wrap(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        real = getattr(metrics, name)
        self.calls[name] = 0

        def spy(*a, **k):  # type: ignore[no-untyped-def]
            self.calls[name] += 1
            return real(*a, **k)

        monkeypatch.setattr(metrics, name, spy)


def test_only_the_bundles_a_rule_set_needs_are_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    run({1: inp(1), 2: inp(2)}, ruleset(rule("quality", "roce", ">", 10)))
    assert spy.calls == {
        "fa_compute": 2, "ta_compute": 0, "valuation_multiples": 0, "detect_flags": 0,
        "classify_setup": 0,
    }  # fmt: skip


def test_cheap_bundles_run_first_and_a_failed_rule_short_circuits_the_expensive_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy(monkeypatch)
    rs = ruleset(
        rule("setup", "setup_type", "==", "pullback"),  # expensive, listed first
        rule("trend", "price_vs_sma200_pct", ">", 0),
        rule("quality", "roce", ">", 100),  # cheap, fails for everyone
    )
    bars = bars_from_closes(ramp_closes(260))
    res = run({i: inp(i, bars=bars) for i in (1, 2, 3)}, rs)
    assert res.matches == () and res.rejected == ()
    assert spy.calls["fa_compute"] == 3
    assert spy.calls["ta_compute"] == 0 and spy.calls["classify_setup"] == 0


def test_string_metric_equality_for_setup_type() -> None:
    up = bars_from_closes(ramp_closes(300, 100, 1))
    down = bars_from_closes(ramp_closes(300, 500, -1))
    universe = {1: inp(1, bars=up), 2: inp(2, bars=down)}
    as_of = up[-1].date
    assert matched(
        run(universe, ruleset(rule("s", "setup_type", "==", "downtrend")), as_of=as_of)
    ) == [2]
    assert matched(
        run(universe, ruleset(rule("s", "setup_type", "!=", "downtrend")), as_of=as_of)
    ) == [1]
    flags = run(
        universe,
        ruleset(rule("b", "base_breakout", "==", 0), rule("p", "pullback_to_50dma", "==", 0)),
        as_of=as_of,
    )
    assert matched(flags) == [1, 2]


def rs_universe(steps: list[int]) -> dict[int, SecurityInputs]:
    bench = bars_from_closes(ramp_closes(300, 100, 1))
    return {
        i: inp(i, bars=bars_from_closes(ramp_closes(300, 100, s)), benchmark=bench)
        for i, s in enumerate(steps, start=1)
    }


def test_rs_percentile_is_computed_over_the_supplied_universe_only_and_says_so() -> None:
    as_of = rs_universe([1])[1].bars[-1].date
    rs = ruleset(rule("rs", "rs_percentile", ">", 50))
    three = run(rs_universe([1, 2, 3]), rs, as_of=as_of)
    assert matched(three) == [3] and "3 securities" in three.universe_basis
    assert abs(three.matches[0].values[0].value - D("83.33333333")) < D("1e-6")  # type: ignore[operator]
    four = run(rs_universe([0, 1, 2, 3]), rs, as_of=as_of)  # a weaker peer lifts the others
    assert matched(four) == [3, 4] and "4 securities" in four.universe_basis


def test_universe_basis_is_reported() -> None:
    res = run({1: inp(1), 2: inp(2)}, ruleset(rule("q", "roce", ">", 10)))
    assert res.evaluated == 2
    assert res.universe_basis.startswith("explicit list of 2 securities")


def test_universe_larger_than_the_configured_cap_is_refused() -> None:
    cfg = AnalysisSettings(screen=ScreenSettings(max_universe=1))
    with pytest.raises(ValueError, match="max_universe"):
        run({1: inp(1), 2: inp(2)}, ruleset(rule("q", "roce", ">", 10)), cfg)


def test_output_is_sorted_by_security_id_and_byte_identical_across_runs() -> None:
    rs = ruleset(rule("q", "roce", ">", 10), rule("m", "net_margin_pct", ">", 5))
    forward = {i: inp(i) for i in (1, 2, 3, 4)}
    backward = {i: inp(i) for i in (4, 3, 2, 1)}
    a, b, c = run(forward, rs), run(forward, rs), run(backward, rs)
    assert matched(a) == [1, 2, 3, 4]
    assert repr(a) == repr(b) == repr(c)


def test_unknown_security_data_gives_reasons_not_exceptions() -> None:
    res = run(
        {1: SecurityInputs(1, "BARE")},
        ruleset(rule("q", "roce", ">", 10), rule("t", "sma_20", ">", 1)),
    )
    assert res.matches == () and {s.rule_id for s in res.skipped} == {"q", "t"}


# ---- the CI budget ---------------------------------------------------------------------------
def test_screen_100_securities_within_the_ci_budget() -> None:
    universe = seed_universe(100)
    rs = ruleset(
        rule("quality", "roce", ">", 10),
        rule("leverage", "net_debt_ebitda", "<", 5),
        rule("trend", "price_vs_sma200_pct", ">", -90),
        rule("cheap", "valuation_percentile", "<", 101),
        rule("revisions", "revisions", ">=", -50),
        rule("setup", "setup_type", "!=", "downtrend"),
        rule("strength", "rs_percentile", ">=", 0),
    )
    start = time.perf_counter()
    res = run(universe, rs, as_of=UNIVERSE_ASOF)
    elapsed = time.perf_counter() - start
    assert res.evaluated == 100 and res.skipped == ()
    assert len(res.matches) == 65 and res.rejected == ()  # fixed by the deterministic data
    assert elapsed < 6, f"100 securities took {elapsed:.1f}s (budget about 6 s per 100)"
