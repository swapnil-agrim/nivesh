from datetime import date
from decimal import Decimal

from nivesh_engine.ratios import ratios
from nivesh_engine.statements import StatementRow

D = Decimal


def row(item: str, value: str, end: date = date(2024, 12, 31), ptype: str = "A") -> StatementRow:
    return StatementRow(period_end=end, period_type=ptype, item=item, value=D(value),  # type: ignore[arg-type]
                        currency="USD", filed_at=date(2025, 2, 1))  # fmt: skip


def test_margins_roe_debt_to_equity_growth() -> None:
    rows = [
        row("revenue", "1000"), row("operating_income", "200"), row("net_income", "100"),
        row("total_equity", "500"), row("total_debt", "250"),
        row("revenue", "800", date(2023, 12, 31)),
    ]  # fmt: skip
    r = ratios(rows)
    assert r.period_end == date(2024, 12, 31) and r.missing == []
    assert r.values == {
        "operating_margin": D("0.2000"), "net_margin": D("0.1000"), "roe": D("0.2000"),
        "debt_to_equity": D("0.5000"), "revenue_growth": D("0.2500"),
    }  # fmt: skip


def test_ratio_with_zero_denominator_is_none_not_error() -> None:
    r = ratios(
        [row("revenue", "0"), row("operating_income", "5"), row("revenue", "0", date(2023, 12, 31))]
    )
    assert r.values["operating_margin"] is None and r.values["revenue_growth"] is None


def test_ratio_skips_missing_items_and_lists_them() -> None:
    r = ratios([row("revenue", "10")])
    assert r.values["roe"] is None
    assert r.missing == [
        "net_income",
        "operating_income",
        "revenue (prior year)",
        "total_debt",
        "total_equity",
    ]
    empty = ratios([], "Q")
    assert empty.period_end is None and set(empty.values) >= {"roe", "revenue_growth"}
    assert ratios([row("net_income", "1")]).missing[-1] == "total_equity"
    assert "revenue" in ratios([row("net_income", "1")]).missing


def test_edgar_companyfacts_fixture_gives_non_null_ratios_and_no_share_only_period() -> None:
    import json
    from pathlib import Path

    from nivesh_engine.statements import facts_from_companyfacts, quarterise

    fx = Path(__file__).resolve().parents[1] / "fixtures/market/edgar_companyfacts.json"
    doc = json.loads(fx.read_text())
    rows = quarterise(facts_from_companyfacts(doc)).rows
    fy = date(2024, 9, 28)
    for ptype in ("A", "Q"):
        periods: dict[date, set[str]] = {}
        for r in rows:
            if r.period_type == ptype and r.currency != "EUR":
                periods.setdefault(r.period_end, set()).add(r.item)
        assert all(items != {"shares_out"} for items in periods.values())
        assert fy in periods and max(periods) == fy
        rs = ratios(rows, ptype)  # type: ignore[arg-type]
        assert rs.period_end == fy
        # the fixture has no quarterly income-statement flows, only balance-sheet items
        keys = ("operating_margin", "net_margin", "roe", "debt_to_equity")
        for k in keys if ptype == "A" else ("debt_to_equity",):
            assert rs.values[k] is not None, (ptype, k)
    shares = [r for r in rows if r.item == "shares_out"]
    assert shares and all(r.period_end == fy for r in shares)


# ---- attributable profit, India only (#131) -----------------------------------------------------
def irow(item: str, value: str, end: date = date(2024, 3, 31), ptype: str = "A") -> StatementRow:
    return StatementRow(period_end=end, period_type=ptype, item=item, value=D(value),  # type: ignore[arg-type]
                        currency="INR", filed_at=date(2025, 2, 1))  # fmt: skip


# consolidated net income 125 against EPS 1 x 100 shares = 100 (+25 percent)
INDIA = [irow("revenue", "1000"), irow("net_income", "125"), irow("total_equity", "500"),
         irow("eps", "1"), irow("shares_out", "100")]  # fmt: skip


def test_india_net_margin_and_roe_use_the_attributable_profit_and_report_the_basis() -> None:
    r = ratios(INDIA, market="IN")
    assert r.values["net_margin"] == D("0.1000") and r.values["roe"] == D("0.2000")
    assert r.profit_basis == "eps_x_shares"
    assert r.missing == ["operating_income", "revenue (prior year)", "total_debt"]


def test_india_quarterly_rows_stay_consolidated_and_labelled_without_a_share_count() -> None:
    q = date(2024, 3, 31)
    items = [("revenue", "250"), ("net_income", "30"), ("total_equity", "500"), ("eps", "0.25")]
    rows = [irow(item, value, q, "Q") for item, value in items]
    r = ratios(rows, "Q", market="IN")
    assert r.values["net_margin"] == D("0.1200") and r.values["roe"] == D("0.0600")
    assert r.profit_basis == "consolidated_minority_unknown"


def test_india_without_eps_keeps_the_consolidated_ratios_labelled_unknown() -> None:
    r = ratios([x for x in INDIA if x.item != "eps"], market="IN")
    assert r.values["net_margin"] == D("0.1250") and r.values["roe"] == D("0.2500")
    assert r.profit_basis == "consolidated_minority_unknown"


def test_india_uses_a_reported_minority_and_a_small_gap_stays_consolidated() -> None:
    reported = ratios([*INDIA, irow("minority_interest", "20")], market="IN")
    assert reported.values["net_margin"] == D("0.1050")
    assert reported.profit_basis == "reported_minority"
    near = ratios([*(x for x in INDIA if x.item != "net_income"), irow("net_income", "102")],
                  market="IN")  # fmt: skip
    assert near.values["net_margin"] == D("0.1020")
    assert near.profit_basis == "consolidated_no_minority_detected"


def test_ratios_outside_india_and_without_a_market_are_unchanged_and_unlabelled() -> None:
    legacy = ratios(INDIA)
    assert legacy.values["net_margin"] == D("0.1250") and legacy.profit_basis is None
    us = ratios([x.model_copy(update={"currency": "USD"}) for x in INDIA], market="US")
    assert us.values == legacy.values and us.profit_basis is None


def test_india_without_net_income_has_no_profit_basis() -> None:
    r = ratios([irow("revenue", "1000"), irow("total_equity", "500")], market="IN")
    assert r.values["net_margin"] is None and r.profit_basis is None
    assert ratios([], market="IN").profit_basis is None
