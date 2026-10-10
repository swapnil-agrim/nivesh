import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_engine.fa import FaResult, fa_compute, sector_kind_of
from nivesh_engine.metric import Metric
from nivesh_engine.statements import (
    CONCEPTS,
    StatementRow,
    facts_from_companyfacts,
    quarterise,
)
from tests.analysis_fx import annual_rows, share_rows, strow

D = Decimal
CFG = AnalysisSettings()
FX = Path(__file__).resolve().parents[1] / "fixtures/market"
ASOF = date(2024, 6, 30)
TOL = D("0.000001")

NEW_ITEMS = {
    "gross_profit", "depreciation_amortization", "interest_expense", "cash", "current_assets",
    "current_liabilities", "receivables", "payables", "inventory", "total_assets",
    "income_tax", "pretax_income", "dividends_per_share",
}  # fmt: skip
OLD_ITEMS = {
    "revenue", "operating_income", "net_income", "eps", "cfo", "capex", "total_debt",
    "total_equity", "shares_out",
}  # fmt: skip

# one US fiscal year with every input, hand-computed answers in the tests below
US_BOOK: dict[str, list[str | int]] = {
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
    "income_tax": [24],
    "pretax_income": [120],
    "cfo": [130],
    "capex": [40],
    "receivables": [125],
    "inventory": [90],
    "payables": [60],
}


def run(
    rows: list[StatementRow],
    *,
    shareholding: list | None = None,
    as_of: date = ASOF,
    kind: str = "general",
    market: str | None = None,
) -> FaResult:
    return fa_compute(
        rows, shareholding or [], as_of=as_of, sector_kind=kind, cfg=CFG, market=market
    )


def us() -> FaResult:
    return run(annual_rows(US_BOOK), market="US")


def get(res: FaResult, name: str) -> Metric:
    assert name in res.metrics, f"{name} missing from {sorted(res.metrics)}"
    return res.metrics[name]


def is_close(res: FaResult, name: str, expected: str, tol: Decimal = TOL) -> None:
    m = get(res, name)
    assert m.available and m.value is not None, (name, m.reason)
    assert abs(m.value - D(expected)) <= tol, (name, m.value, expected)


def unavailable(res: FaResult, name: str, *needles: str) -> Metric:
    m = get(res, name)
    assert not m.available and m.value is None and m.reason, name
    for n in needles:
        assert n.lower() in m.reason.lower(), (name, m.reason, n)
    return m


# ---- vocabulary ---------------------------------------------------------------------------------
def test_us_concepts_extended_additively_and_existing_items_unchanged() -> None:
    assert set(CONCEPTS) == OLD_ITEMS | NEW_ITEMS
    assert CONCEPTS["revenue"] == (
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
    )  # fmt: skip
    assert CONCEPTS["operating_income"] == ("OperatingIncomeLoss",)
    assert CONCEPTS["capex"] == ("PaymentsToAcquirePropertyPlantAndEquipment",)


def test_edgar_fixture_yields_the_new_items() -> None:
    doc = json.loads((FX / "edgar_companyfacts_ext.json").read_text())
    rows = quarterise(facts_from_companyfacts(doc)).rows
    got = {r.item: r for r in rows if r.period_type == "A"}
    assert set(got) == NEW_ITEMS
    assert got["gross_profit"].value == D(180683)
    assert got["dividends_per_share"].value == D(1) and got["cash"].value == D(29943)
    assert got["pretax_income"].value == D(123485) and got["income_tax"].value == D(29749)
    assert got["total_assets"].period_end == date(2024, 9, 28)
    old = json.loads((FX / "edgar_companyfacts.json").read_text())
    assert "GrossProfit" not in json.dumps(old)  # the original fixture is left alone


# ---- growth -------------------------------------------------------------------------------------
def test_revenue_cagr_3y_known_answer() -> None:
    res = run(annual_rows({"revenue": [1100, 1210, 1331, "1464.1"]}))
    is_close(res, "revenue_cagr_3y_pct", "10")
    assert get(res, "revenue_cagr_3y_pct").inputs["points"] == 4


def test_cagr_needs_n_plus_one_annual_points_else_unavailable_with_reason() -> None:
    res = run(annual_rows({"revenue": [1100, 1210, 1331], "net_income": [0, 100, 50, 80]}))
    unavailable(res, "revenue_cagr_3y_pct", "need 4 annual points", "have 3")
    unavailable(res, "net_income_cagr_3y_pct", "positive")  # a zero start has no growth rate


def test_last_four_quarters_yoy_known_answer() -> None:
    rows = [
        strow("revenue", v, e, ptype="Q")
        for v, e in [
            (100, date(2022, 3, 31)),
            (100, date(2022, 6, 30)),
            (100, date(2022, 9, 30)),
            (100, date(2022, 12, 31)),
            (110, date(2023, 3, 31)),
            (120, date(2023, 6, 30)),
            (90, date(2023, 9, 30)),
            (150, date(2023, 12, 31)),
        ]  # fmt: skip
    ]
    res = run(rows)
    for name, want in [("q0", "50"), ("q1", "-10"), ("q2", "20"), ("q3", "10")]:
        is_close(res, f"revenue_yoy_{name}_pct", want)
    assert get(res, "revenue_yoy_q0_pct").inputs["period_end"] == date(2023, 12, 31)
    short = run(rows[4:])
    unavailable(short, "revenue_yoy_q0_pct", "prior-year")


def test_growth_consistency_std_dev_known_answer() -> None:
    res = run(annual_rows({"revenue": [100, 110, 132, 132]}))
    is_close(res, "revenue_growth_stdev_pp", "10")
    few = run(annual_rows({"revenue": [100, 110]}))
    unavailable(few, "revenue_growth_stdev_pp", "need 4 annual points")


# ---- profitability ------------------------------------------------------------------------------
def test_margins_known_answer_gross_ebitda_net() -> None:
    res = us()
    is_close(res, "gross_margin_pct", "40")
    is_close(res, "operating_margin_pct", "15")
    is_close(res, "ebitda_margin_pct", "20")
    is_close(res, "net_margin_pct", "10")


def test_ebitda_is_operating_income_plus_da_and_unavailable_without_da() -> None:
    book = {k: v for k, v in US_BOOK.items() if k != "depreciation_amortization"}
    res = run(annual_rows(book), market="US")
    unavailable(res, "ebitda_margin_pct", "depreciation_amortization")
    unavailable(res, "net_debt_ebitda", "depreciation_amortization")
    is_close(res, "operating_margin_pct", "15")


def test_roe_on_average_equity_known_answer() -> None:
    res = us()
    is_close(res, "roe_pct", "20")  # 100 / ((450 + 550) / 2)
    assert get(res, "roe_pct").inputs["average_equity"] == D(500)
    only_one = run(annual_rows({"net_income": [100], "total_equity": [550]}))
    unavailable(only_one, "roe_pct", "prior-year equity")


def test_roic_us_known_answer_with_effective_tax_assumption_listed() -> None:
    res = us()
    is_close(res, "roic_pct", "20")  # 150 * (1 - 24/120) / (200 + 550 - 150)
    m = get(res, "roic_pct")
    assert m.inputs["tax_rate_pct"] == D(20) and m.inputs["tax_source"] == "filing"
    book = {k: v for k, v in US_BOOK.items() if k not in ("income_tax", "pretax_income")}
    dflt = get(run(annual_rows(book), market="US"), "roic_pct")
    assert dflt.inputs["tax_source"] == "default_assumption"
    assert dflt.inputs["tax_rate_pct"] == CFG.fa.default_tax_rate_pct
    is_close(run(annual_rows(book), market="US"), "roic_pct", "18.75")  # 150 * 0.75 / 600
    is_close(res, "roce_pct", "18.75")  # 150 / (1000 - 200)


def test_negative_equity_makes_roe_unavailable() -> None:
    book = dict(US_BOOK)
    book["total_equity"] = [450, -10]
    res = run(annual_rows(book), market="US")
    unavailable(res, "roe_pct", "non-positive equity")
    unavailable(res, "debt_equity", "non-positive equity")


# ---- balance sheet and cash quality -------------------------------------------------------------
def test_net_debt_ebitda_debt_equity_interest_cover_current_ratio_known_answers() -> None:
    res = us()
    is_close(res, "net_debt_ebitda", "0.25")  # (200 - 150) / 200
    is_close(res, "debt_equity", "0.363636363636")
    is_close(res, "interest_cover", "6")
    is_close(res, "current_ratio", "1.5")


def test_cfo_to_pat_fcf_margin_accruals_working_capital_days_known_answers() -> None:
    res = us()
    is_close(res, "cfo_to_pat", "1.3")
    is_close(res, "fcf_margin_pct", "9")
    is_close(res, "accruals_ratio_pct", "-3")
    is_close(res, "receivable_days", "45.625")
    is_close(res, "inventory_days", "54.75")
    is_close(res, "payable_days", "36.5")
    is_close(res, "working_capital_days", "63.875")


def test_capex_sign_is_normalised_positive_outflow() -> None:
    fact = {"start": "2023-01-01", "end": "2023-12-31", "val": -40, "filed": "2024-02-15",
            "form": "10-K"}  # fmt: skip
    doc = {"facts": {"us-gaap": {"PaymentsToAcquirePropertyPlantAndEquipment":
                                 {"units": {"USD": [fact]}}}}}  # fmt: skip
    capex = [r for r in quarterise(facts_from_companyfacts(doc)).rows if r.item == "capex"]
    assert [r.value for r in capex] == [D(40)]
    book = {"revenue": [1000], "cfo": [130], "capex": [-40]}
    res = run(annual_rows(book), market="US")
    is_close(res, "fcf_margin_pct", "9")


# ---- look-ahead ---------------------------------------------------------------------------------
def look_rows() -> list[StatementRow]:
    rows = annual_rows({"revenue": [800, 1000], "net_income": [80, 100]}, last_fy=2023)
    return rows


def test_as_of_excludes_rows_filed_after_it() -> None:
    rows = look_rows()  # FY2022 filed 2023-02-15, FY2023 filed 2024-02-15
    early = run(rows, as_of=date(2023, 12, 31))
    late = run(rows, as_of=date(2024, 2, 15))
    assert early.fiscal_year_end == date(2022, 12, 31) and early.data_through == date(2023, 2, 15)
    assert late.fiscal_year_end == date(2023, 12, 31) and late.data_through == date(2024, 2, 15)
    assert get(early, "net_margin_pct").inputs["revenue"] == D(800)
    assert get(late, "net_margin_pct").inputs["revenue"] == D(1000)


def test_restatement_filed_after_as_of_is_ignored_and_the_earlier_value_used() -> None:
    rows = [*look_rows(), strow("net_income", 120, date(2023, 12, 31), date(2024, 9, 1))]
    before = run(rows, as_of=date(2024, 6, 30))
    after = run(rows, as_of=date(2024, 12, 31))
    assert get(before, "net_margin_pct").inputs["net_income"] == D(100)
    assert get(after, "net_margin_pct").inputs["net_income"] == D(120)
    assert after.data_through == date(2024, 9, 1) and before.data_through == date(2024, 2, 15)


def test_period_ending_before_as_of_but_filed_after_is_excluded() -> None:
    res = run(look_rows(), as_of=date(2023, 1, 31))  # FY2022 ended 2022-12-31, filed 2023-02-15
    assert res.fiscal_year_end is None and res.data_through is None
    unavailable(res, "net_margin_pct", "no annual statements filed on or before 2023-01-31")
    assert res.coverage_pct == D("0.00")


def test_shareholding_filed_after_as_of_is_excluded() -> None:
    sh = share_rows([("50", "10"), ("51", "10"), ("52", "11"), ("53", "12"), ("54", "13"),
                     ("60", "40")])  # fmt: skip
    last = sh[-1]
    res = run([], shareholding=sh, as_of=last.filed_at.replace(day=1), market="IN")
    full = run([], shareholding=sh, as_of=last.filed_at, market="IN")
    is_close(full, "promoter_change_4q_pp", "9")  # 60 - 51, five quarters back
    is_close(full, "promoter_pledged_pct", "40")
    assert get(res, "promoter_pledged_pct").inputs["period_end"] == sh[-2].period_end
    is_close(res, "promoter_change_4q_pp", "4")  # 54 - 50


def test_as_of_is_echoed_in_the_output() -> None:
    res = run(look_rows(), as_of=date(2024, 5, 5))
    assert res.as_of == date(2024, 5, 5)


# ---- banks and NBFCs ----------------------------------------------------------------------------
def bank_rows() -> list[StatementRow]:
    book: dict[str, list[str | int]] = {
        "revenue": [900, 1000], "net_income": [90, 100], "total_equity": [450, 550],
        "gnpa_pct": ["2.5", "2.0"], "nnpa_pct": ["0.8", "0.6"], "nim_pct": ["3.4", "3.5"],
        "casa_pct": ["42", "44"], "car_pct": ["17.5", "18"],
    }  # fmt: skip
    return annual_rows(book, currency="INR")


def test_financial_kind_replaces_ebitda_based_metrics_with_bank_metrics() -> None:
    res = run(bank_rows(), kind="financial", market="IN")
    for name, want in [("gnpa_pct", "2"), ("nnpa_pct", "0.6"), ("nim_pct", "3.5"),
                       ("casa_pct", "44"), ("car_pct", "18")]:  # fmt: skip
        is_close(res, name, want)
    for gone in ("ebitda_margin_pct", "net_debt_ebitda", "interest_cover", "current_ratio",
                 "roic_pct", "roce_pct", "working_capital_days", "cfo_to_pat"):  # fmt: skip
        assert gone not in res.metrics, gone
    is_close(res, "roe_pct", "20")
    general = run(bank_rows(), kind="general", market="IN")
    assert "gnpa_pct" not in general.metrics and res.sector_kind == "financial"


def test_credit_cost_unavailable_with_reason_no_provision_item() -> None:
    res = run(bank_rows(), kind="financial", market="IN")
    unavailable(res, "credit_cost_pct", "no provision item")


def test_sector_kind_from_configured_sector_names_or_presence_of_bank_items() -> None:
    plain = annual_rows({"revenue": [1000]})
    assert sector_kind_of("Banks", plain, CFG) == "financial"
    assert sector_kind_of("nbfc", plain, CFG) == "financial"
    assert sector_kind_of("Energy", plain, CFG) == "general"
    assert sector_kind_of(None, plain, CFG) == "general"
    assert sector_kind_of(None, bank_rows(), CFG) == "financial"
    only_nim = [strow("nim_pct", "3.5", date(2023, 12, 31), currency="INR")]
    assert sector_kind_of("Energy", only_nim, CFG) == "financial"


# ---- honesty ------------------------------------------------------------------------------------
def test_missing_field_gives_available_false_reason_and_inputs() -> None:
    book = {k: v for k, v in US_BOOK.items() if k != "gross_profit"}
    res = run(annual_rows(book), market="US")
    m = unavailable(res, "gross_margin_pct", "gross_profit")
    assert m.inputs["revenue"] == D(1000)
    is_close(res, "net_margin_pct", "10")


def test_coverage_pct_counts_available_over_applicable_metrics() -> None:
    res = us()
    counted = [m for m in res.metrics.values() if not (m.reason or "").startswith("not applicable")]
    have = sum(1 for m in counted if m.available)
    assert res.coverage_pct == (D(100) * have / len(counted)).quantize(D("0.01"))
    assert D(0) < res.coverage_pct < D(100)  # growth and ownership are unavailable here
    thin = run(annual_rows({"revenue": [1000], "net_income": [100]}), market="US")
    assert thin.coverage_pct is not None and thin.coverage_pct < res.coverage_pct


def test_india_ebit_based_and_cash_metrics_unavailable_pbt_never_used_as_ebit() -> None:
    book: dict[str, list[str | int]] = {
        "revenue": [1000], "operating_income": [150], "net_income": [100],
        "depreciation_amortization": [50], "interest_expense": [25], "cfo": [130],
        "total_assets": [1000], "current_assets": [300], "current_liabilities": [200],
        "total_debt": [200], "total_equity": [450, 550],
    }  # fmt: skip
    res = run(annual_rows(book, currency="INR"), market="IN")
    for name in ("operating_margin_pct", "ebitda_margin_pct", "roic_pct", "roce_pct",
                 "net_debt_ebitda", "interest_cover"):  # fmt: skip
        unavailable(res, name, "profit before tax")
    for name in ("current_ratio", "cfo_to_pat", "fcf_margin_pct", "accruals_ratio_pct",
                 "working_capital_days"):  # fmt: skip
        unavailable(res, name, "india")
    is_close(res, "net_margin_pct", "10")
    is_close(res, "debt_equity", "0.363636363636")


def test_zero_denominator_is_unavailable_never_zero() -> None:
    book = dict(US_BOOK)
    book["revenue"] = [0]
    res = run(annual_rows(book), market="US")
    for name in ("gross_margin_pct", "net_margin_pct", "ebitda_margin_pct", "fcf_margin_pct",
                 "receivable_days"):  # fmt: skip
        unavailable(res, name, "zero")


def test_ownership_trend_promoter_and_pledge_india_and_not_applicable_for_us() -> None:
    sh = share_rows([("50", "10"), ("51", "10"), ("52", "11"), ("53", "12"), ("54", "13")])
    asof = sh[-1].filed_at
    india = run([], shareholding=sh, as_of=asof, market="IN")
    is_close(india, "promoter_change_4q_pp", "4")
    is_close(india, "promoter_pledged_pct", "13")
    short = run([], shareholding=sh[2:], as_of=asof, market="IN")
    unavailable(short, "promoter_change_4q_pp", "need 5 quarters")
    us_res = run([], shareholding=sh, as_of=asof, market="US")
    unavailable(us_res, "promoter_change_4q_pp", "not applicable")
    unavailable(us_res, "promoter_pledged_pct", "not applicable")


def test_fa_compute_is_deterministic() -> None:
    rows = annual_rows(US_BOOK)
    assert run(rows, market="US") == run(list(reversed(rows)), market="US")
    res = us()
    for m in res.metrics.values():
        assert m.value is None or isinstance(m.value, Decimal)
