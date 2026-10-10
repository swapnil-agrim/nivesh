import dataclasses
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from nivesh_core.mf_models import FundHoldingRow
from nivesh_engine.mf_valuation import history_compare, weighted_multiple

D = Decimal
A, B, C, E = "INE000A01010", "INE111A01011", "INE222B01012", "INE333C01013"


def rows(*lines: tuple[str, str, str]) -> list[FundHoldingRow]:
    return [
        FundHoldingRow(
            month_end=date(2025, 12, 31),
            isin=i,
            weight_pct=D(w),
            kind=k,
            source="mf_holdings",  # type: ignore[arg-type]
        )
        for i, w, k in lines
    ]


def test_weighted_pe_harmonic_known_answer() -> None:
    r = weighted_multiple(
        rows((A, "40", "equity"), (B, "40", "equity"), (C, "20", "equity")),
        {A: D("10"), B: D("20"), C: None},
        D("50"),
    )
    # 80 / (40/10 + 40/20) = 13.3333; the stock without a multiple is excluded, not zeroed
    assert r.value == D("13.3333") and (r.used, r.total) == (2, 3) and r.coverage_pct == D("80.00")


def test_weighted_pb_known_answer() -> None:
    r = weighted_multiple(
        rows((A, "50", "equity"), (B, "50", "equity")), {A: D("2"), B: D("4")}, D("90")
    )
    assert r.value == D("2.6667") and r.coverage_pct == D("100.00")  # 100 / (25 + 12.5)


def test_negative_or_missing_multiple_excluded_and_coverage_reported() -> None:
    r = weighted_multiple(
        rows(
            (A, "60", "equity"),
            (B, "20", "equity"),
            (C, "10", "equity"),
            ("OTHER:CASH", "10", "other"),
        ),
        {A: D("15"), B: D("-5"), C: D("0")},
        D("50"),
    )
    assert r.value == D("15.0000") and (r.used, r.total) == (1, 3)
    assert r.coverage_pct == D("66.67")  # 60 of the 90 equity weight; cash is not equity


def test_below_min_coverage_is_unavailable_with_reason() -> None:
    r = weighted_multiple(rows((A, "30", "equity"), (B, "70", "equity")), {A: D("10")}, D("60"))
    assert r.value is None and r.coverage_pct == D("30.00")
    assert "cover 30.00%" in (r.reason or "") and "minimum 60" in (r.reason or "")


def test_all_inputs_missing_never_zero() -> None:
    none = weighted_multiple(rows((A, "50", "equity")), {}, D("10"))
    assert none.value is None and none.coverage_pct == D("0.00") and none.used == 0
    assert "positive multiple" in (none.reason or "")
    empty = weighted_multiple(rows(("OTHER:CASH", "5", "other")), {}, D("10"))
    assert (
        empty.value is None and empty.coverage_pct is None and empty.reason == "no equity holdings"
    )
    assert weighted_multiple([], {}, D("10")).value is None


def months(n: int, value: str = "20") -> dict[date, Decimal | None]:
    out: dict[date, Decimal | None] = {}
    for i in range(n):
        y, m = divmod(2023 * 12 + 11 - (n - 1) + i, 12)  # n consecutive month ends ending Dec 2025
        out[date(y, m + 1, 28)] = D(value) + (i % 5)  # 20, 21, 22, 23, 24, 20, ...
    return out


def test_history_compare_against_own_36_month_median_min_max() -> None:
    h = months(40)  # only the latest 36 count
    h[min(h)] = D("999")  # an old outlier outside the window must not matter
    r = history_compare(h, D("26"), D("1.25"))
    assert r.months_used == 36 and r.reason is None
    assert (r.minimum, r.maximum) == (D("20.0000"), D("24.0000"))
    assert r.median == D("22.0000") and r.ratio == D("1.1818")  # 26 / 22
    assert r.label == "in range"


def test_fewer_than_24_months_unavailable_with_count() -> None:
    r = history_compare(months(23), D("20"), D("1.25"))
    assert r.ratio is None and r.median is None and r.months_used == 23
    assert r.reason == "only 23 usable month(s) of history (need 24)"
    gaps = months(30)
    for d in list(gaps)[:10]:
        gaps[d] = None
    assert history_compare(gaps, D("20"), D("1.25")).months_used == 20


def test_current_vs_history_label_cheap_expensive_by_config_ratio() -> None:
    h = months(36)
    assert history_compare(h, D("30"), D("1.25")).label == "expensive"  # 30 / 22 = 1.36
    assert history_compare(h, D("16"), D("1.25")).label == "cheap"  # 16 / 22 = 0.73 < 0.8
    assert history_compare(h, D("22"), D("1.25")).label == "in range"
    assert history_compare(h, D("30"), D("1.5")).label == "in range"  # a looser owner ratio


def test_current_unavailable_keeps_history_stats_but_no_ratio() -> None:
    r = history_compare(months(30), None, D("1.25"))
    assert r.ratio is None and r.label is None and r.median is not None
    assert r.reason == "current multiple unavailable"
    assert history_compare(months(30), D("-2"), D("1.25")).ratio is None


def test_valuation_deterministic() -> None:
    args = (rows((A, "40", "equity"), (B, "60", "equity")), {A: D("11"), B: D("17")}, D("50"))
    a, b = weighted_multiple(*args), weighted_multiple(*args)
    assert json.dumps(dataclasses.asdict(a), default=str) == json.dumps(
        dataclasses.asdict(b), default=str
    )
    src = (Path(__file__).resolve().parents[2] / "nivesh_engine" / "mf_valuation.py").read_text()
    for banned in ("float(", "datetime.now", "date.today", "import random"):
        assert banned not in src


def test_stock_multiples_point_in_time_pe_and_pb() -> None:
    from nivesh_engine.mf_valuation import stock_multiples
    from nivesh_engine.statements import StatementRow

    def row(item: str, value: str, end: date, filed: date) -> StatementRow:
        return StatementRow(
            period_end=end,
            period_type="A",
            item=item,
            value=D(value),
            currency="INR",
            filed_at=filed,
        )

    rs = [
        row("eps", "10", date(2023, 3, 31), date(2023, 5, 1)),
        row("eps", "20", date(2024, 3, 31), date(2024, 5, 1)),
        row("total_equity", "1000", date(2024, 3, 31), date(2024, 5, 1)),
        row("shares_out", "10", date(2024, 3, 31), date(2024, 5, 1)),
    ]
    assert stock_multiples(rs, D("100"), date(2024, 12, 31)) == (D("5.0000"), D("1.0000"))
    # before the FY24 filing only the older EPS is known, and there is no book data yet
    assert stock_multiples(rs, D("100"), date(2023, 12, 31)) == (D("10.0000"), None)
    assert stock_multiples(rs, D("100"), date(2022, 12, 31)) == (None, None)
    assert stock_multiples(rs, None, date(2024, 12, 31)) == (None, None)
    loss = [row("eps", "-3", date(2024, 3, 31), date(2024, 5, 1))]
    assert stock_multiples(loss, D("100"), date(2024, 12, 31)) == (None, None)
    q = StatementRow(period_end=date(2024, 6, 30), period_type="Q", item="eps", value=D(1),
                     currency="INR", filed_at=date(2024, 8, 1))  # fmt: skip
    assert stock_multiples([q], D("100"), date(2024, 12, 31)) == (None, None)
