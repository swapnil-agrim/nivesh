from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.mf_models import FundHoldingRow
from nivesh_engine.mf_overlap import (
    DirectEquity,
    OwnedFund,
    look_through,
    overlap_matrix,
    pairwise_overlap,
)

D = Decimal
I1, I2, I3, I4, I5 = (
    "INE000A01010", "INE111A01011", "INE222B01012", "INE333C01013", "INE444D01014",
)  # fmt: skip
MONTH = date(2025, 12, 31)


def rows(*lines: tuple[str, str, str]) -> list[FundHoldingRow]:
    return [
        FundHoldingRow(month_end=MONTH, isin=i, weight_pct=D(w), kind=k, source="mf_holdings")  # type: ignore[arg-type]
        for i, w, k in lines
    ]


A = rows(
    (I1, "30", "equity"), (I2, "20", "equity"), (I3, "10", "equity"), ("OTHER:CASH", "5", "other")
)
B = rows(
    (I1, "10", "equity"), (I2, "40", "equity"), (I4, "15", "equity"), ("OTHER:CASH", "7", "other")
)
SECTORS = {I1: "Energy", I2: "Banks", I3: None, I4: "Banks", I5: "Energy"}


def test_pairwise_overlap_sum_of_min_weights_known_answer() -> None:
    o = pairwise_overlap(A, B)
    assert (o.overlap_pct, o.common_isins) == (D("30"), 2)  # min(30, 10) + min(20, 40)


def test_overlap_is_symmetric_and_bounded_0_to_100() -> None:
    assert pairwise_overlap(A, B) == pairwise_overlap(B, A)
    full = rows((I1, "60", "equity"), (I2, "40", "equity"))
    assert D(0) <= pairwise_overlap(A, B).overlap_pct <= D(100)
    assert pairwise_overlap(full, full).overlap_pct == D("100")


def test_identical_funds_overlap_is_total_weight() -> None:
    assert pairwise_overlap(A, A).overlap_pct == D("60")  # equity weight only: 30 + 20 + 10


def test_disjoint_funds_overlap_zero_with_common_isin_count_zero() -> None:
    other = rows((I4, "50", "equity"), (I5, "50", "equity"))
    assert pairwise_overlap(A, other) == pairwise_overlap(other, A)
    o = pairwise_overlap(A, other)
    assert (o.overlap_pct, o.common_isins) == (D("0"), 0)
    assert pairwise_overlap(A, []).overlap_pct == D("0")


def test_other_lines_never_count_as_overlap() -> None:
    assert pairwise_overlap(rows(("OTHER:CASH", "5", "other")), B).common_isins == 0


def test_matrix_deterministic_order_and_symmetric() -> None:
    c = rows((I1, "5", "equity"))
    m = overlap_matrix({"300": c, "100": A, "200": B})
    assert m.codes == ["100", "200", "300"]
    assert m.get("100", "200") == m.get("200", "100") == pairwise_overlap(A, B)
    assert ("100", "100") not in m.cells and len(m.cells) == 6
    assert overlap_matrix({"200": B, "100": A, "300": c}) == m


FUNDS = [OwnedFund("100", "Fund A", D("100000")), OwnedFund("200", "Fund B", D("200000"))]
HOLD = {"100": A, "200": B}
DIRECT = [DirectEquity(I1, "Alpha", D("50000")), DirectEquity(I5, "Epsilon", D("20000"))]
TOTAL = D("370000")


def test_look_through_combines_fund_and_direct_equity_for_same_isin() -> None:
    lt = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL)
    first = next(s for s in lt.stocks if s.isin == I1)
    assert first.exposure_inr == D("100000.00")
    assert [(c.source, c.exposure_inr) for c in first.contributions] == [
        ("100", D("30000.00")), ("200", D("20000.00")), ("direct", D("50000.00")),
    ]  # fmt: skip


def test_exposure_inr_and_pct_of_portfolio_known_answer() -> None:
    lt = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL)
    got = {s.isin: (s.exposure_inr, s.exposure_pct) for s in lt.stocks}
    assert got[I1] == (D("100000.00"), D("27.03"))  # 100000 / 370000
    assert got[I2] == (D("100000.00"), D("27.03"))  # 20000 + 80000
    assert got[I3] == (D("10000.00"), D("2.70"))
    assert got[I4] == (D("30000.00"), D("8.11"))
    assert got[I5] == (D("20000.00"), D("5.41"))
    assert lt.stock_count == 5


def test_top_20_stocks_limit_and_isin_tiebreak() -> None:
    lines = [(f"INE{n:03d}A01010", "2", "equity") for n in range(25)]
    lt = look_through(
        [OwnedFund("100", "Fund A", D("1000"))], {"100": rows(*lines)}, {}, [], D("1000")
    )
    assert len(lt.stocks) == 20 and lt.stock_count == 25
    assert [s.isin for s in lt.stocks] == sorted(s.isin for s in lt.stocks)  # all tied: by ISIN
    assert lt.stocks[0].isin == "INE000A01010"
    tie = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL).stocks
    assert [s.isin for s in tie[:2]] == [I1, I2]  # equal exposure, ISIN ascending


def test_sector_exposure_sums_and_reports_unmapped_sector_pct() -> None:
    lt = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL)
    assert [(s.sector, s.exposure_inr, s.exposure_pct) for s in lt.sectors] == [
        ("Banks", D("130000.00"), D("35.14")), ("Energy", D("120000.00"), D("32.43")),
    ]  # fmt: skip
    assert (lt.unmapped_sector_inr, lt.unmapped_sector_pct) == (D("10000.00"), D("2.70"))
    equity = sum((s.exposure_inr for s in lt.sectors), D(0)) + lt.unmapped_sector_inr
    assert equity == D("260000.00")


def test_other_lines_bucketed_not_counted_as_stock() -> None:
    lt = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL)
    assert (lt.other_inr, lt.other_pct) == (D("19000.00"), D("5.14"))  # 5000 + 14000
    assert all(not s.isin.startswith("OTHER") for s in lt.stocks)


def test_fund_with_unavailable_value_excluded_and_named() -> None:
    funds = [*FUNDS, OwnedFund("300", "Fund C", None), OwnedFund("400", "Fund D", D("1"))]
    lt = look_through(funds, {**HOLD, "300": A}, SECTORS, DIRECT, TOTAL)
    got = {e.amfi_code: e.reason for e in lt.excluded}
    assert got == {"300": "current value unavailable", "400": "no stored holdings"}
    assert lt.stock_count == 5  # the excluded funds add nothing


def test_nested_fund_line_lands_in_other() -> None:
    nested = rows((I1, "50", "equity"), ("INF000A01011", "30", "other"))
    lt = look_through(
        [OwnedFund("100", "Fund A", D("1000"))], {"100": nested}, SECTORS, [], D("1000")
    )
    assert lt.other_inr == D("300.00") and lt.stock_count == 1


def test_stock_contributions_list_sources() -> None:
    lt = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL)
    i5 = next(s for s in lt.stocks if s.isin == I5)
    assert [c.source for c in i5.contributions] == ["direct"] and i5.contributions[
        0
    ].name == "Epsilon"
    assert lt.month_ends == {"100": MONTH, "200": MONTH}


def test_look_through_deterministic_rerun_identical() -> None:
    a = look_through(FUNDS, HOLD, SECTORS, DIRECT, TOTAL)
    b = look_through(
        list(reversed(FUNDS)), dict(reversed(list(HOLD.items()))), SECTORS, DIRECT[::-1], TOTAL
    )
    assert a == b


def test_non_positive_total_rejected_and_no_float_or_clock() -> None:
    with pytest.raises(ValueError, match="total"):
        look_through(FUNDS, HOLD, SECTORS, DIRECT, D(0))
    src = (Path(__file__).resolve().parents[2] / "nivesh_engine" / "mf_overlap.py").read_text()
    for banned in ("float(", "datetime.now", "date.today", "import random"):
        assert banned not in src
