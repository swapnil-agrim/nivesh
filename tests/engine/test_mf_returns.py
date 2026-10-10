import dataclasses
import json
import math
import statistics
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_engine.mf_returns import BENCHMARK_LABEL, IDCW_REASON, analyse_fund
from tests.mf_fx import series, synthetic_series

D = Decimal
ROOT = Path(__file__).resolve().parents[2]

# Sparse series whose windows are exactly 365 days: annualisation exponent 1, so every window
# return is end / start - 1 and can be written out by hand.
FUND = series([(0, "100"), (100, "110"), (365, "121"), (465, "99"), (730, "150")])
BENCH = series([(0, "1000"), (100, "1010"), (365, "1100"), (465, "1111"), (730, "1500")])


def one(fund=FUND, bench=BENCH, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("windows", [365])
    return analyse_fund(fund, bench, **kw)


def test_rolling_returns_known_answer() -> None:
    w = one().rolling[0]
    # windows end on day 365 (21%), 465 (-10%) and 730 (29/121 = 23.9669%)
    assert w.windows == 3
    assert (w.median_return_pct, w.min_return_pct, w.max_return_pct) == (
        D("21.0000"), D("-10.0000"), D("23.9669"),
    )  # fmt: skip


def test_window_without_start_point_skipped() -> None:
    # the first two points (day 0 and 100) have no point a full 365 days earlier
    assert one().rolling[0].windows == 3 and len(FUND) == 5


def test_beat_pct_known_answer() -> None:
    w = one().rolling[0]
    # excess: +11, -20, -12.3967 -> one of three windows beats the benchmark
    assert w.beat_pct == D("33.33")


def test_median_excess_odd_and_even_window_counts() -> None:
    assert one().rolling[0].median_excess_pct == D("-12.3967")  # odd: middle of -20, -12.3967, 11
    fund6 = [*FUND, *series([(830, "165")])]
    bench6 = [*BENCH, *series([(830, "1222.1")])]
    w = one(fund6, bench6).rolling[0]
    # sixth window: 165/99 - 1 = 66.6667% vs 10% -> excess 56.6667; median of four = -169/242
    assert w.windows == 4 and w.median_excess_pct == D("-0.6983")
    assert w.beat_pct == D("50.00")


def test_elapsed_days_drive_the_exponent_not_the_nominal_window() -> None:
    f = series([(0, "100"), (400, "121"), (800, "146.41")])
    w = one(f, None).rolling[0]
    # both windows span 400 days: 1.21 ** (365 / 400) - 1 = 18.9985 percent, not 21 percent
    assert w.windows == 2 and w.median_return_pct == D("18.9985")


def test_insufficient_history_is_unavailable_with_reason_never_zero() -> None:
    r = one(FUND[:3], BENCH[:3])
    w = r.rolling[0]
    assert w.windows == 1 and w.median_return_pct is None and w.beat_pct is None
    assert "insufficient history" in (w.reason or "")
    empty = analyse_fund([], None, windows=[365])
    assert empty.rolling[0].median_return_pct is None and empty.std_dev_pct is None
    assert empty.max_drawdown is None and empty.sortino is None


def test_std_dev_annualised_known_answer_uses_decimal_sqrt() -> None:
    nav = series([(0, "100"), (1, "110"), (2, "99"), (3, "108.9"), (4, "100.188")])
    r = one(nav, None)
    rets = [0.1, -0.1, 0.1, -0.08]  # sample std dev * sqrt(252), in percent
    assert r.std_dev_pct == D("174.6196")
    assert abs(float(r.std_dev_pct) - statistics.stdev(rets) * math.sqrt(252) * 100) < 1e-4


def test_max_drawdown_value_and_dates() -> None:
    nav = series([(0, "100"), (1, "120"), (2, "90"), (3, "110"), (4, "80"), (5, "130")])
    dd = one(nav, None).max_drawdown
    assert dd is not None and dd.pct == D("33.3333")  # 120 -> 80
    assert (dd.peak_date, dd.trough_date) == (date(2020, 1, 2), date(2020, 1, 5))
    flat = one(series([(0, "100"), (1, "101"), (2, "102")]), None).max_drawdown
    assert flat is not None and flat.pct == D("0.0000")


DAILY_F = series([(0, "100"), (1, "103"), (2, "102.485"), (3, "101.46015"), (4, "103.489353")])
DAILY_B = series([(0, "1000"), (1, "1020"), (2, "1009.8"), (3, "989.604"), (4, "999.50004")])


def test_downside_capture_known_answer() -> None:
    r = one(DAILY_F, DAILY_B)
    # benchmark falls on days 2 and 3: fund -0.5%, -1% against -1%, -2% -> 50 percent
    assert r.downside_capture_pct == D("50.0000")


def test_downside_capture_unavailable_without_down_days() -> None:
    up = series([(0, "1000"), (1, "1010"), (2, "1020"), (3, "1030"), (4, "1040")])
    r = one(DAILY_F, up)
    assert r.downside_capture_pct is None and "no benchmark down days" in (
        r.downside_capture_reason or ""
    )


def test_sortino_known_answer_with_mar() -> None:
    # daily returns 3%, -0.5%, -1%, 2%
    assert one(DAILY_F, None).sortino == D("24.8475")
    assert one(DAILY_F, None, mar_pct=D("6")).sortino == D("23.4990")


def test_sortino_unavailable_when_downside_deviation_zero() -> None:
    up = series([(0, "100"), (1, "101"), (2, "102.01"), (3, "103.0301")])
    r = one(up, None)
    assert r.sortino is None and r.sortino_reason == "downside deviation is zero"


def test_benchmark_misaligned_dates_use_common_dates_and_report_coverage() -> None:
    bench = [p for i, p in enumerate(BENCH) if i != 1]  # one date missing
    r = one(FUND, bench)
    assert r.coverage_pct == D("80.00")
    assert r.rolling[0].beat_pct is not None
    strict = one(FUND, bench, min_alignment_pct=D("90"))
    assert strict.rolling[0].beat_pct is None and strict.downside_capture_pct is None
    assert "covers 80.00%" in (strict.rolling[0].relative_reason or "")
    assert strict.rolling[0].median_return_pct == D("21.0000")  # fund-only metrics unaffected


def test_benchmark_missing_marks_relative_metrics_unavailable_other_metrics_still_computed() -> (
    None
):
    r = one(FUND, None)
    w = r.rolling[0]
    assert w.beat_pct is None and w.median_excess_pct is None
    assert w.relative_reason == "benchmark series missing"
    assert (
        r.downside_capture_pct is None and r.downside_capture_reason == "benchmark series missing"
    )
    assert w.median_return_pct == D("21.0000") and r.std_dev_pct is not None
    assert r.max_drawdown is not None and r.coverage_pct is None


def test_idcw_option_returns_not_comparable_reason() -> None:
    r = one(option="idcw")
    assert not r.comparable and r.reason == IDCW_REASON and r.rolling == []
    assert r.std_dev_pct is None and r.max_drawdown is None and r.sortino is None


def test_as_of_drops_later_points_and_inputs_are_validated() -> None:
    r = one(as_of=date(2020, 1, 1) + timedelta(days=465))
    assert r.points == 4 and r.rolling[0].windows == 2
    with pytest.raises(ValueError, match="ascending"):
        analyse_fund([(date(2020, 1, 2), D(1)), (date(2020, 1, 1), D(1))], None, windows=[1])
    with pytest.raises(ValueError, match="> 0"):
        analyse_fund([(date(2020, 1, 1), D(0))], None, windows=[1])


def test_real_size_three_and_five_year_windows() -> None:
    fund = synthetic_series(2300)
    bench = synthetic_series(2300, base=900, drift=5, wobble=14, phase=41)
    r = analyse_fund(fund, bench, windows=[1095, 1826], mar_pct=D("5"))
    assert r.coverage_pct == D("100.00") and r.comparable
    dates = [d for d, _ in fund]
    for w in r.rolling:
        expected = sum(1 for d in dates if d - timedelta(days=w.window_days) >= dates[0])
        assert w.windows == expected > 100
        assert w.median_return_pct is not None and w.beat_pct is not None
        assert D(0) <= w.beat_pct <= D(100)
        assert w.min_return_pct <= w.median_return_pct <= w.max_return_pct  # type: ignore[operator]
    # independent float oracle for the 3y median
    import bisect

    navs = [v for _, v in fund]
    cagr = []
    for j, d in enumerate(dates):
        i = bisect.bisect_right(dates, d - timedelta(days=1095)) - 1
        if i >= 0:
            years = (d - dates[i]).days / 365
            cagr.append((float(navs[j] / navs[i]) ** (1 / years) - 1) * 100)
    assert abs(float(r.rolling[0].median_return_pct or 0) - statistics.median(cagr)) < 1e-3  # type: ignore[arg-type]
    assert r.max_drawdown is not None and r.std_dev_pct is not None and r.sortino is not None


def test_same_input_twice_is_byte_identical() -> None:
    fund, bench = synthetic_series(1600), synthetic_series(1600, base=800, drift=4, phase=29)
    a = analyse_fund(fund, bench, windows=[1095])
    b = analyse_fund(list(fund), list(bench), windows=[1095])
    dump = lambda x: json.dumps(dataclasses.asdict(x), default=str, sort_keys=True)  # noqa: E731
    assert dump(a) == dump(b)


def walk(obj: object) -> list[object]:
    if dataclasses.is_dataclass(obj):
        return [v for f in dataclasses.fields(obj) for v in walk(getattr(obj, f.name))]
    if isinstance(obj, list):
        return [v for x in obj for v in walk(x)]
    return [obj]


def test_result_is_decimal_everywhere_no_float() -> None:
    r = one(DAILY_F, DAILY_B)
    leaves = walk(r)
    assert not [x for x in leaves if isinstance(x, float)]
    assert all(
        isinstance(x, Decimal)
        for x in leaves
        if x is not None and not isinstance(x, str | int | bool | date)
    )
    assert r.benchmark_label == BENCHMARK_LABEL == "price index, not TRI"


def test_no_clock_or_random_imports() -> None:
    src = (ROOT / "nivesh_engine" / "mf_returns.py").read_text()
    for banned in (
        "import random",
        "datetime.now",
        "date.today",
        "utcnow",
        "import time",
        "float(",
    ):
        assert banned not in src, banned


def test_mf_modules_do_not_import_xirr_or_lot_report() -> None:
    files = [*(ROOT / "nivesh_engine").glob("mf_*.py"), ROOT / "nivesh_engine" / "fund_doctor.py"]
    files += [ROOT / "nivesh_engine" / "fund_screen.py"]
    files += [ROOT / "nivesh_adapters" / n for n in ("nav.py", "mf_data.py", "mf_ingest.py")]
    for f in files:
        if f.exists():
            text = f.read_text()
            assert "xirr" not in text and "lot_report" not in text, f.name
