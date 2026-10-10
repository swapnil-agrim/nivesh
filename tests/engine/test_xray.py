from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import MarketCapBands, XraySettings
from nivesh_core.mf_models import FundHoldingRow
from nivesh_engine.consolidate import consolidate
from nivesh_engine.mf_lots import ValuedLot, ValuedLots
from nivesh_engine.mf_overlap import DirectEquity, OwnedFund, look_through
from nivesh_engine.xray import (
    HoldingFlows,
    mf_holding_flows,
    portfolio_xray,
    us_holding_flows,
)
from tests.analysis_fx import make_profile, xrow
from tests.holdings_fx import holding
from tests.us_fx import lot, usdinr_obs

D = Decimal
CFG = XraySettings(
    market_cap={
        "IN": MarketCapBands(large_min=D("100000"), mid_min=D("20000")),
        "US": MarketCapBands(large_min=D("200"), mid_min=D("20")),
    }
)
START, END = date(2023, 1, 1), date(2024, 1, 1)  # 365 days apart


def xray(rows, *, profile=None, cfg=CFG, sector=None, cap=None, cat=None, flows=None, lt=None):  # type: ignore[no-untyped-def]
    return portfolio_xray(
        rows,
        profile or make_profile(),
        sector_of=sector or {},
        market_cap_of=cap or {},
        mf_category_of=cat or {},
        flows=flows or {},
        look_through=lt,
        cfg=cfg,
    )


def weights(buckets):  # type: ignore[no-untyped-def]
    return {b.name: b.weight_pct for b in buckets}


def close(a: Decimal | None, b: str, tol: str = "1e-6") -> bool:
    return a is not None and abs(a - D(b)) <= D(tol)


def test_allocation_by_asset_class_market_sector_market_cap_and_currency_each_sum_to_100() -> None:
    rows = [
        xrow("A", 400),
        xrow("B", 300),
        xrow("C", 100, "mf"),
        xrow("D", 100, currency="USD"),
        xrow("E", 100, "bond"),
    ]
    x = xray(
        rows,
        sector={"A": "IT", "B": "Banks", "D": "Tech"},
        cap={"A": D("200000"), "B": D("50000"), "D": D("500")},
        cat={"C": "Equity: Large Cap"},
    )
    assert set(x.allocation) == {"asset_class", "market", "sector", "market_cap", "currency"}
    for buckets in x.allocation.values():
        assert close(sum((b.weight_pct for b in buckets), D(0)), "100", "1e-6")
    a = x.allocation
    assert weights(a["asset_class"]) == {"equity": D(90), "debt": D(10)}
    assert weights(a["market"]) == {"IN": D(90), "US": D(10)}
    assert weights(a["currency"]) == {"INR": D(90), "USD": D(10)}
    assert weights(a["sector"]) == {
        "IT": D(40),
        "Banks": D(30),
        "Tech": D(10),
        "unclassified": D(20),
    }
    assert weights(a["market_cap"]) == {"large": D(50), "mid": D(30), "unclassified": D(20)}
    assert x.total_inr == D(1000)


def test_rows_without_inr_value_are_excluded_and_listed_with_weight_reported() -> None:
    x = xray([xrow("A", 300), xrow("B", 100), xrow("U", None, currency="USD")])
    assert [(e.key, "INR" in e.reason) for e in x.excluded] == [("U", True)]
    assert x.included == 2 and x.total_inr == D(400)
    assert {p.key: p.weight_pct for p in x.positions} == {"A": D(75), "B": D(25)}


def test_unclassified_bucket_is_reported_for_sector_and_market_cap_never_dropped() -> None:
    x = xray([xrow("A", 100), xrow("B", 100)], sector={"A": "IT"}, cap={"A": D("200000")})
    assert weights(x.allocation["sector"]) == {"IT": D(50), "unclassified": D(50)}
    assert weights(x.allocation["market_cap"]) == {"large": D(50), "unclassified": D(50)}


def test_market_cap_bucket_from_owner_thresholds_per_market() -> None:
    rows = [xrow(k, 100) for k in ("at_large", "below_large", "below_mid", "us_mid", "us_large")]
    rows += [xrow("no_bands", 100, currency="EUR")]
    cap = {
        "at_large": D("100000"), "below_large": D("99999.99"), "below_mid": D("19999"),
        "us_mid": D("199"), "us_large": D("200"), "no_bands": D("10"),
    }  # fmt: skip
    market = {"us_mid": "US", "us_large": "US", "no_bands": "EU"}
    x = portfolio_xray(
        rows, make_profile(), sector_of={}, market_cap_of=cap, mf_category_of={}, flows={},
        look_through=None, cfg=CFG, market_of=market,
    )  # fmt: skip
    got = weights(x.allocation["market_cap"])
    sixth = D(100) / 6
    expected = {"large": 2 * sixth, "mid": 2 * sixth, "small": sixth, "unclassified": sixth}
    assert set(got) == set(expected)
    assert all(abs(got[k] - v) < D("1e-6") for k, v in expected.items())
    assert {b.name for b in x.allocation["market"]} == {"IN", "US", "EU"}


def test_asset_class_mapping_table_mf_category_equity_debt_and_gold_unclassified() -> None:
    rows = [
        xrow("eq_mf", 100, "mf"), xrow("debt_mf", 100, "mf"), xrow("liq_mf", 100, "mf"),
        xrow("gold_mf", 100, "mf"), xrow("etf", 100, "etf"), xrow("gold", 100, "gold"),
        xrow("nocat", 100, "mf"),
    ]  # fmt: skip
    cat = {
        "eq_mf": "equity scheme - flexi cap", "debt_mf": "Debt: Gilt", "liq_mf": "Liquid Fund",
        "gold_mf": "Gold ETF FoF",
    }  # fmt: skip
    x = xray(rows, cat=cat)
    got = weights(x.allocation["asset_class"])
    seven = D(100) / 7
    assert abs(got["equity"] - 2 * seven) < D("1e-6")  # equity mf + etf
    assert abs(got["debt"] - 2 * seven) < D("1e-6")  # debt mf + liquid
    assert abs(got["unclassified"] - 3 * seven) < D("1e-6")  # gold fund, gold, no category


def test_drift_is_actual_minus_target_for_the_union_of_classes() -> None:
    x = xray([xrow("A", 900), xrow("B", 100, "bond")])
    drift = {d.asset_class: (d.actual_pct, d.target_pct, d.drift_pp) for d in x.drift}
    assert drift == {
        "equity": (D(90), D(60), D(30)),
        "debt": (D(10), D(30), D(-20)),
        "gold": (D(0), D(10), D(-10)),
    }
    assert [d.asset_class for d in x.drift] == ["debt", "equity", "gold"]


def test_top_5_and_top_10_weight_known_answer() -> None:
    values = [30, 20, 15, 10, 8, 6, 5, 3, 2, 1]
    x = xray([xrow(f"S{i}", v) for i, v in enumerate(values)])
    assert x.concentration.top == {5: D(83), 10: D(100)}
    assert [p.weight_pct for p in x.positions][:3] == [D(30), D(20), D(15)]


def test_hhi_and_effective_positions_known_answer_50_30_20() -> None:
    x = xray([xrow("A", 50), xrow("B", 30), xrow("C", 20)])
    assert close(x.concentration.hhi, "0.38", "1e-9")
    assert close(x.concentration.effective_positions, "2.631579", "1e-6")


def test_positions_over_max_position_pct_strict_greater_than() -> None:
    x = xray(
        [xrow("A", 50), xrow("B", 30), xrow("C", 20)], profile=make_profile(max_position_pct=30)
    )
    assert [p.key for p in x.concentration.positions_over_limit] == ["A"]  # 30 is not over 30
    assert x.concentration.max_position_pct == D(30)


def lookthrough_with_banks_at_35():  # type: ignore[no-untyped-def]
    isin_bank, isin_it = "INE000A01010", "INE111A01011"
    month = date(2025, 12, 31)
    lines = [
        FundHoldingRow(
            month_end=month, isin=isin_bank, weight_pct=D(70), kind="equity", source="t"
        ),
        FundHoldingRow(month_end=month, isin=isin_it, weight_pct=D(10), kind="equity", source="t"),
        FundHoldingRow(month_end=month, isin="CASH", weight_pct=D(20), kind="other", source="t"),
    ]
    return look_through(
        [OwnedFund("100", "Fund", D(500)), OwnedFund("200", "Empty", None)],
        {"100": lines},
        {isin_bank: "Banks", isin_it: None},
        [DirectEquity(isin_it, "ItCo", D(500))],
        D(1000),
    )


def test_sectors_over_limit_direct_and_look_through_basis_separate() -> None:
    lt = lookthrough_with_banks_at_35()  # Banks 350 of 1000 = 35 percent
    x = xray(
        [xrow("A", 400), xrow("B", 300), xrow("C", 300, "mf")],
        sector={"A": "IT", "B": "Banks"},
        lt=lt,
    )
    assert [b.name for b in x.concentration.sectors_over_limit] == ["IT"]  # 40 > 30, Banks is 30
    assert [b.name for b in x.concentration.look_through_sectors_over_limit] == ["Banks"]
    assert x.concentration.look_through_sectors_over_limit[0].weight_pct == D("35")


def test_look_through_exposure_is_included_with_unmapped_and_excluded_fields() -> None:
    lt = lookthrough_with_banks_at_35()
    x = xray([xrow("A", 1000)], lt=lt)
    assert x.look_through is lt
    assert x.look_through is not None
    assert x.look_through.unmapped_sector_pct == D("55.00")  # ItCo 500 + 10% of 500
    assert [e.amfi_code for e in x.look_through.excluded] == ["200"]
    assert xray([xrow("A", 1000)]).look_through is None


def test_xirr_per_us_holding_from_lot_flows_in_inr() -> None:
    fx = usdinr_obs(("2023-01-01", "80"), ("2024-01-01", "80"))
    hf = us_holding_flows(
        [lot(acquired_on=START, quantity=D(10), cost_per_unit=D(100))],
        D(10), D(1200), END, fx, "usdinr",
    )  # fmt: skip
    assert hf.coverage == "exact" and hf.reason is None
    assert hf.flows == ((START, D(-80000)), (END, D(96000)))
    x = xray([xrow("AAPL", 96000, currency="USD")], flows={"AAPL": hf})
    r = x.returns[0]
    assert r.coverage == "exact" and close(r.xirr, "0.2")


def test_us_flows_report_partial_over_and_missing_rate_without_flows() -> None:
    fx = usdinr_obs(("2023-01-01", "80"), ("2024-01-01", "80"))
    one = [lot(acquired_on=START, quantity=D(10), cost_per_unit=D(100))]
    partial = us_holding_flows(one, D(15), D(1800), END, fx, "usdinr")
    over = us_holding_flows(one, D(5), D(600), END, fx, "usdinr")
    no_rate = us_holding_flows(one, D(10), D(1200), END, None, "usdinr")
    none = us_holding_flows([], D(10), D(1200), END, fx, "usdinr")
    assert (partial.coverage, over.coverage) == ("partial", "over")
    assert partial.flows and not over.flows and "over" in (over.reason or "")
    assert no_rate.flows == () and "USDINR" in (no_rate.reason or "")
    assert none.coverage == "none" and none.flows == ()


def valued(units: str, cost: str | None, value: str, reason: str | None = None) -> ValuedLots:
    c = None if cost is None else D(cost)
    lots = [ValuedLot(START, D(units), None, 365, D(value), c, None)]
    return ValuedLots(lots, reason)


def test_xirr_per_mf_holding_from_valued_lots() -> None:
    hf = mf_holding_flows(valued("100", "1000", "1200"), D(100), END)
    assert hf.coverage == "exact" and hf.flows == ((START, D(-1000)), (END, D(1200)))
    x = xray([xrow("F", 1200, "mf")], flows={"F": hf})
    assert close(x.returns[0].xirr, "0.2")


def test_mf_flows_coverage_and_unavailable_reasons() -> None:
    assert mf_holding_flows(valued("100", "1000", "1200"), D("100.0005"), END).coverage == "exact"
    assert mf_holding_flows(valued("100", "1000", "1200"), D(120), END).coverage == "partial"
    assert mf_holding_flows(valued("100", "1000", "1200"), D(80), END).coverage == "over"
    unknown_cost = mf_holding_flows(valued("100", None, "1200"), D(100), END)
    assert unknown_cost.flows == () and "cost" in (unknown_cost.reason or "")
    stale = mf_holding_flows(valued("100", "1000", "1200", "no stored NAV"), D(100), END)
    assert stale.flows == () and stale.reason == "no stored NAV"
    nothing = mf_holding_flows(ValuedLots([], None), D(100), END)
    assert nothing.coverage == "none" and nothing.flows == ()


def test_indian_direct_equity_xirr_unavailable_reason_no_lot_dates() -> None:
    x = xray([xrow("RELI", 100)])
    r = x.returns[0]
    assert r.xirr is None and r.coverage == "none" and "no lot dates" in (r.reason or "")
    assert x.total_return.xirr is None and x.total_return.coverage_pct == D(0)


def flows_for(cost: int, value: int) -> HoldingFlows:
    return HoldingFlows(((START, D(-cost)), (END, D(value))), "exact")


def test_only_exact_coverage_holdings_contribute_to_total_xirr() -> None:
    flows = {
        "A": flows_for(1000, 1100),
        "B": HoldingFlows(((START, D(-1000)), (END, D(5000))), "partial", "dated lots cover part"),
        "C": HoldingFlows(((START, D(-1000)), (END, D(9000))), "over", "dated lots exceed"),
    }
    x = xray([xrow("A", 1100), xrow("B", 5000), xrow("C", 9000)], flows=flows)
    t = x.total_return
    assert t.pooled == ("A",) and close(t.xirr, "0.1")
    assert {k for k, _ in t.withheld} == {"B", "C"}
    by_key = {r.key: r for r in x.returns}
    assert by_key["B"].xirr is None and by_key["B"].coverage == "partial"
    assert by_key["C"].xirr is None and "exceed" in (by_key["C"].reason or "")


def test_total_xirr_reports_coverage_pct_of_portfolio_value() -> None:
    x = xray(
        [xrow("A", 400), xrow("B", 600)],
        flows={"A": flows_for(350, 400)},
    )
    assert x.total_return.coverage_pct == D(40)
    assert ("B", "no lot dates") in x.total_return.withheld


def test_total_xirr_matches_pooled_flow_known_answer_within_0_01_pct() -> None:
    flows = {"A": flows_for(1000, 1100), "B": flows_for(1000, 1300)}
    x = xray([xrow("A", 1100), xrow("B", 1300)], flows=flows)
    by_key = {r.key: r.xirr for r in x.returns}
    assert close(by_key["A"], "0.1") and close(by_key["B"], "0.3")
    assert x.total_return.xirr is not None
    assert abs(x.total_return.xirr - D("0.2")) / D("0.2") < D("0.0001")  # pooled 2000 -> 2400
    assert x.total_return.coverage_pct == D(100)


def test_over_or_partial_coverage_holding_is_reported_not_silently_included() -> None:
    flows = {"P": HoldingFlows(((START, D(-1)), (END, D(2))), "partial", "partial lots")}
    x = xray([xrow("P", 100)], flows=flows)
    assert x.total_return.xirr is None and x.total_return.pooled == ()
    assert x.total_return.withheld == (("P", "partial lots"),)


def test_empty_portfolio_and_zero_total_do_not_raise() -> None:
    for rows in ([], [xrow("Z", 0)], [xrow("U", None)]):
        x = xray(rows)
        assert x.total_inr == D(0)
        assert x.concentration.hhi is None and x.concentration.top == {}
        assert x.total_return.xirr is None
    assert xray([xrow("Z", 0)]).positions[0].weight_pct == D(0)


def test_weights_use_fractions_from_consolidate_rows_correctly() -> None:
    book = consolidate(
        [
            holding(isin="INE000A01010", symbol="AAA", quantity=D(10), price=D(30)),
            holding(isin="INE111A01011", symbol="BBB", quantity=D(10), price=D(70)),
        ]
    )
    assert sum((r.weight or D(0)) for r in book.rows) == D(1)  # fractions, not percent
    x = xray(list(book.rows))
    assert {p.key: p.weight_pct for p in x.positions} == {
        r.key: 100 * (r.weight or D(0)) for r in book.rows
    }
    assert sum((p.weight_pct for p in x.positions), D(0)) == D(100)


def test_xray_is_deterministic() -> None:
    rows = [xrow("A", 400), xrow("B", 400), xrow("C", 200, "bond")]
    flows = {"A": flows_for(300, 400)}
    a = xray(rows, flows=flows, sector={"A": "IT"})
    b = xray(list(reversed(rows)), flows=flows, sector={"A": "IT"})
    assert a == b
    assert [p.key for p in a.positions] == ["A", "B", "C"]  # ties break on the key
