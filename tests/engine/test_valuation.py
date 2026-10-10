from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import AnalysisSettings, DcfScenario
from nivesh_engine.metric import Metric
from nivesh_engine.statements import StatementRow
from nivesh_engine.valuation import (
    PeerMultiples,
    ValuationInputs,
    ValuationResult,
    current_multiples,
    dcf_value,
    valuation_multiples,
    valuation_range,
)
from tests.analysis_fx import annual_rows, month_ends, quarter_rows, strow

D = Decimal
CFG = AnalysisSettings()
TOL = D("0.000001")
ASOF = date(2024, 6, 30)


def cfg_with(**valuation: object) -> AnalysisSettings:
    return CFG.model_copy(update={"valuation": CFG.valuation.model_copy(update=valuation)})


def inputs(
    rows: list[StatementRow],
    price: str | None = "50",
    *,
    ends: list[tuple[date, Decimal]] | None = None,
    market: str = "US",
    kind: str = "general",
    as_of: date = ASOF,
    price_date: date = date(2024, 6, 28),
) -> ValuationInputs:
    last = None if price is None else (price_date, D(price))
    return ValuationInputs(market, kind, as_of, tuple(rows), tuple(ends or ()), last)


def series(n: int, price_of, as_of_month: tuple[int, int] = (2024, 6)):  # type: ignore[no-untyped-def]
    """`n` month-end closes ending at the given month; price_of(i) is the close of the i-th."""
    y, m = as_of_month
    total = y * 12 + (m - 1) - (n - 1)
    first = date(total // 12, total % 12 + 1, 1)
    return [(e, D(price_of(i))) for i, e in enumerate(month_ends(first, n))]


def close(m: Metric, expected: str | Decimal, tol: Decimal = TOL) -> None:
    assert m.available and m.value is not None, m.reason
    assert abs(m.value - D(expected)) <= tol, (m.value, expected)


def na_with(m: Metric, *needles: str) -> None:
    assert not m.available and m.value is None and m.reason
    for n in needles:
        assert n.lower() in m.reason.lower(), (m.reason, n)


US_FY: dict[str, list[str | int]] = {
    "operating_income": [150], "depreciation_amortization": [50], "total_debt": [200],
    "cash": [150], "total_equity": [550], "cfo": [130], "capex": [40],
    "dividends_per_share": [2], "eps": [5],
}  # fmt: skip


def us_rows(**over: list[str | int]) -> list[StatementRow]:
    book = {**US_FY, **over}
    return [
        *annual_rows(book),
        strow("shares_out", 100, date(2023, 12, 31), date(2024, 2, 15), "Q"),
    ]


# ---- price to earnings --------------------------------------------------------------------------
def test_pe_from_price_and_ttm_eps_known_answer() -> None:
    rows = [*us_rows(), *quarter_rows("eps", ["1.25"] * 4, date(2023, 3, 31))]
    pe = current_multiples(inputs(rows))["pe"]
    close(pe, "10")
    assert pe.inputs["eps_basis"] == "ttm" and pe.inputs["eps"] == D(5)


def test_pe_uses_last_annual_eps_when_fewer_than_four_quarters_and_is_labelled() -> None:
    rows = [*annual_rows({"eps": [4]}), *quarter_rows("eps", ["1"] * 2, date(2023, 9, 30))]
    pe = current_multiples(inputs(rows))["pe"]
    close(pe, "12.5")
    assert pe.inputs["eps_basis"] == "last_annual"


def test_negative_eps_is_not_meaningful_and_excluded_from_history() -> None:
    eps = ["1"] * 24 + ["-10"] * 2  # 2018Q1.. positive, then losses from 2024Q1
    rows = quarter_rows("eps", eps, date(2018, 3, 31))
    res = valuation_multiples(
        inputs(rows, "50", ends=series(24, lambda i: 100)),
        None,
        cfg=cfg_with(min_obs=5, history_years=[1]),
    )
    pe = res.multiples["pe"]
    na_with(pe.current, "non-positive")
    na_with(pe.percentiles["1y"], "current")
    # May and June 2024 have a negative trailing EPS and drop out of the 12-month window
    assert pe.medians["1y"].inputs["observations"] == 10
    close(pe.medians["1y"], "25")  # price 100 over TTM EPS 4


def test_history_observation_uses_only_statements_filed_by_that_month_end() -> None:
    rows = annual_rows({"eps": [8, 10]}, last_fy=2023)  # FY2022 filed 2023-02-15, FY2023 2024-02-15
    ends = series(17, lambda i: 80)  # month ends from 2023-02-28 to 2024-06-30
    res = valuation_multiples(
        inputs(rows, "80", ends=ends), None, cfg=cfg_with(min_obs=10, history_years=[1])
    )
    pe = res.multiples["pe"]
    close(pe.current, "8")  # 80 / 10
    assert pe.medians["1y"].inputs["observations"] == 12
    close(pe.medians["1y"], "10")  # seven months at 80/8, five at 80/10
    close(pe.percentiles["1y"], D(100) * 5 / 12)


# ---- history statistics -------------------------------------------------------------------------
def long_history() -> ValuationInputs:
    rows = quarter_rows("eps", [1] * 50, date(2012, 3, 31))
    ends = series(132, lambda i: 100 + i)
    return inputs(rows, "220", ends=ends, as_of=date(2024, 7, 15), price_date=date(2024, 7, 12))


def test_median_5y_and_10y_known_answer() -> None:
    pe = valuation_multiples(long_history(), None, cfg=CFG).multiples["pe"]
    close(pe.current, "55")
    close(pe.medians["5y"], "50.375")  # month ends 2019-07 to 2024-06: closes 172 to 231
    close(pe.medians["10y"], "42.875")  # month ends 2014-07 to 2024-06: closes 112 to 231
    assert pe.medians["5y"].inputs["observations"] == 60
    assert pe.medians["10y"].inputs["observations"] == 120


def test_percentile_is_share_of_observations_at_or_below_current_known_answer() -> None:
    pe = valuation_multiples(long_history(), None, cfg=CFG).multiples["pe"]
    close(pe.percentiles["5y"], D(100) * 49 / 60)  # closes 172 to 220 are at or below 220
    close(pe.percentiles["10y"], D(100) * 109 / 120)


def test_insufficient_history_gives_reason_with_observation_count() -> None:
    rows = quarter_rows("eps", [1] * 30, date(2017, 3, 31))
    pe = valuation_multiples(inputs(rows, "120", ends=series(12, lambda i: 100)), None, cfg=CFG)
    m = pe.multiples["pe"]
    na_with(m.medians["5y"], "12 observations", "need 24")
    na_with(m.percentiles["5y"], "12 observations", "need 24")


def test_10y_unavailable_when_history_is_shorter_with_span_in_reason() -> None:
    rows = quarter_rows("eps", [1] * 30, date(2017, 3, 31))
    res = valuation_multiples(
        inputs(rows, "120", ends=series(72, lambda i: 100 + i)), None, cfg=CFG
    )
    m = res.multiples["pe"]
    assert m.medians["5y"].available and not m.medians["10y"].available
    na_with(m.medians["10y"], "history spans", "need 10")
    na_with(m.percentiles["10y"], "history spans")


# ---- peers --------------------------------------------------------------------------------------
def test_peer_median_known_answer_with_min_peers() -> None:
    rows = [*us_rows(), *quarter_rows("eps", ["1.25"] * 4, date(2023, 3, 31))]
    peers = PeerMultiples({"pe": (D(12), D(8), D(10))}, considered=3)
    m = valuation_multiples(inputs(rows), peers, cfg=CFG).multiples["pe"].peer_median
    close(m, "10")
    assert m.inputs["peers"] == 3


def test_insufficient_peers_reason() -> None:
    rows = us_rows()
    peers = PeerMultiples({"pe": (D(12), D(8))}, considered=5)
    m = valuation_multiples(inputs(rows), peers, cfg=CFG).multiples["pe"].peer_median
    na_with(m, "2 peers", "need 3")
    none = valuation_multiples(inputs(rows), None, cfg=CFG).multiples["pe"].peer_median
    na_with(none, "no peer")
    empty = PeerMultiples({}, 0, "no industry recorded for this security")
    na_with(
        valuation_multiples(inputs(rows), empty, cfg=CFG).multiples["pb"].peer_median, "industry"
    )


# ---- other multiples ----------------------------------------------------------------------------
def test_ev_ebitda_fcf_yield_dividend_yield_and_pb_us_known_answers() -> None:
    m = current_multiples(inputs(us_rows()))  # price 50, 100 shares: market cap 5000
    close(m["pe"], "10")
    close(m["pb"], D(5000) / 550)
    close(m["ev_ebitda"], "25.25")  # (5000 + 200 - 150) / (150 + 50)
    close(m["fcf_yield_pct"], "1.8")  # (130 - 40) / 5000
    close(m["dividend_yield_pct"], "4")  # 2 / 50


def test_financials_report_pb_and_pe_not_ev_ebitda() -> None:
    m = current_multiples(inputs(us_rows(), kind="financial"))
    assert m["pe"].available and m["pb"].available
    na_with(m["ev_ebitda"], "financial")
    na_with(m["fcf_yield_pct"], "financial")


def test_india_multiples_without_inputs_unavailable_with_reason() -> None:
    rows = annual_rows({"eps": [5], "total_equity": [550], "total_debt": [200],
                        "operating_income": [150]}, currency="INR")  # fmt: skip
    m = current_multiples(inputs(rows, market="IN"))
    close(m["pe"], "10")
    na_with(m["pb"], "shares_out")
    na_with(m["ev_ebitda"], "shares_out")
    na_with(m["fcf_yield_pct"], "shares_out")
    na_with(m["dividend_yield_pct"], "dividends_per_share")


def test_no_close_gives_unavailable_multiples() -> None:
    m = current_multiples(inputs(us_rows(), price=None))
    for cell in m.values():
        na_with(cell, "close")
    res = valuation_multiples(inputs(us_rows(), price=None), None, cfg=CFG)
    assert res.price is None and res.market_cap is None


# ---- reverse DCF and the fair-value range -------------------------------------------------------
def fcf_rows(cfo: int = 120, capex: int = 20, shares: int | None = 10) -> list[StatementRow]:
    rows = annual_rows({"cfo": [cfo], "capex": [capex]})
    if shares is not None:
        rows.append(strow("shares_out", shares, date(2023, 12, 31), date(2024, 2, 15), "Q"))
    return rows


def flat_cfg() -> AnalysisSettings:
    def sc(r: str) -> DcfScenario:
        return DcfScenario(growth_pct=D(0), discount_pct=D(r), terminal_growth_pct=D(0))

    dcf = CFG.valuation.dcf.model_copy(
        update={"base": sc("10"), "bull": sc("8"), "bear": sc("12.5")}
    )
    return cfg_with(dcf=dcf)


def test_reverse_dcf_flat_cash_flow_invariant_value_is_cf_over_r() -> None:
    assert abs(dcf_value(D(100), D(0), D("0.1"), D(0), 10) - D(1000)) < D("1e-20")
    assert abs(dcf_value(D(100), D(0), D("0.1"), D(0), 3) - D(1000)) < D("1e-20")
    res = valuation_range(inputs(fcf_rows(), "100"), cfg=flat_cfg())  # market cap 100 x 10 = 1000
    close(res.implied_growth, "0", D("1e-8"))


def test_reverse_dcf_recovers_a_known_growth_rate() -> None:
    cf, g, r, gt, n = D(100), D("0.05"), D("0.1"), D("0.02"), 5
    q = (1 + g) / (1 + r)
    pv = cf * q * (1 - q**n) / (1 - q)  # closed form of the growing annuity
    tv = cf * (1 + g) ** n * (1 + gt) / (r - gt) / (1 + r) ** n
    cap = pv + tv
    dcf = CFG.valuation.dcf.model_copy(
        update={
            "horizon_years": n,
            "base": DcfScenario(growth_pct=D(8), discount_pct=D(10), terminal_growth_pct=D(2)),
        }
    )
    res = valuation_range(inputs(fcf_rows(120, 20, 1), str(cap)), cfg=cfg_with(dcf=dcf))
    close(res.implied_growth, "5", D("0.0001"))
    assert res.implied_growth.inputs["discount_pct"] == D(10)


def test_reverse_dcf_none_for_non_positive_cash_flow_with_reason() -> None:
    res = valuation_range(inputs(fcf_rows(10, 20), "100"), cfg=CFG)
    na_with(res.implied_growth, "non-positive cash flow")
    for sc in res.scenarios.values():
        na_with(sc.equity_value, "non-positive cash flow")
    na_with(res.cash_flow, "non-positive")


def test_reverse_dcf_unreachable_market_cap_reason() -> None:
    high = valuation_range(inputs(fcf_rows(), "1000000000000"), cfg=CFG)
    na_with(high.implied_growth, "exceeds", "100%")
    low = valuation_range(inputs(fcf_rows(), "0.0000001"), cfg=CFG)
    na_with(low.implied_growth, "below", "-50%")


def test_fair_value_range_base_bull_bear_known_answer_and_ordering() -> None:
    res = valuation_range(inputs(fcf_rows(), "60"), cfg=flat_cfg())
    per = {k: v.per_share for k, v in res.scenarios.items()}
    close(per["bear"], "80")
    close(per["base"], "100")
    close(per["bull"], "125")
    assert per["bear"].value < per["base"].value < per["bull"].value  # type: ignore[operator]
    close(res.scenarios["base"].equity_value, "1000")
    close(res.cash_flow, "100")
    assert res.cash_flow.inputs["basis"] == "fcf"
    assert tuple(res.scenarios) == ("bear", "base", "bull")


def test_range_echoes_every_assumption() -> None:
    res = valuation_range(inputs(fcf_rows(), "60"), cfg=CFG)
    a = res.assumptions
    assert a["horizon_years"] == 10 and a["cash_flow_basis"] == "fcf"
    assert a["shares_out"] == D(10) and a["market_cap"] == D(600)
    scenarios = a["scenarios"]
    assert isinstance(scenarios, dict)
    for name in ("bear", "base", "bull"):
        sc = getattr(CFG.valuation.dcf, name)
        assert scenarios[name] == {
            "growth_pct": sc.growth_pct,
            "discount_pct": sc.discount_pct,
            "terminal_growth_pct": sc.terminal_growth_pct,
        }
    assert res.as_of == ASOF


def test_per_share_value_needs_share_count_else_equity_value_only_with_reason() -> None:
    res = valuation_range(inputs(fcf_rows(shares=None), "60"), cfg=flat_cfg())
    base = res.scenarios["base"]
    close(base.equity_value, "1000")
    na_with(base.per_share, "shares_out", "equity value only")
    na_with(res.implied_growth, "market cap")


def test_financials_use_earnings_as_the_cash_flow() -> None:
    rows = [
        *annual_rows({"net_income": [100]}),
        strow("shares_out", 10, date(2023, 12, 31), ptype="Q"),
    ]
    res = valuation_range(inputs(rows, "60", kind="financial"), cfg=flat_cfg())
    close(res.cash_flow, "100")
    assert res.cash_flow.inputs["basis"] == "earnings"
    close(res.scenarios["base"].per_share, "100")


def test_valuation_is_deterministic() -> None:
    inp = long_history()
    a, b = valuation_multiples(inp, None, cfg=CFG), valuation_multiples(inp, None, cfg=CFG)
    assert a == b and isinstance(a, ValuationResult)
    assert valuation_range(inp, cfg=CFG) == valuation_range(inp, cfg=CFG)
    for r in a.multiples.values():
        assert r.current.value is None or isinstance(r.current.value, Decimal)
