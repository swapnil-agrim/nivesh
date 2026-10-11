"""Scoring rules, hand-computed in Decimal. Dates: the call is issued on Monday 2026-01-05, so
one week ends 2026-01-12 and four weeks 2026-02-02."""

from datetime import date
from decimal import Decimal

import pytest

from nivesh_engine.ledger_score import ScoreResult, Series, score_call, target_date

D = Decimal
ISSUE = date(2026, 1, 5)
WEEK1 = date(2026, 1, 12)
LATE = date(2027, 1, 1)  # a "today" well past every grace window


def ser(*pairs: tuple[date, str | int]) -> Series:
    return Series.of([(d, D(str(c))) for d, c in pairs])


def two(sec_end: str, bench_end: str, sec_start: str = "100", bench_start: str = "200"):  # type: ignore[no-untyped-def]
    return (
        ser((ISSUE, sec_start), (WEEK1, sec_end)),
        ser((ISSUE, bench_start), (WEEK1, bench_end)),
    )


def score(verdict: str, sec: Series, bench: Series, **kw: object) -> ScoreResult | None:
    args: dict[str, object] = dict(
        as_of=ISSUE, price_date=ISSUE, horizon_weeks=1, cost_pct=D("0.25"), today=LATE,
        stale_days=7,
    )  # fmt: skip
    args.update(kw)
    return score_call(verdict, security=sec, benchmark=bench, **args)  # type: ignore[arg-type]


def test_buy_beating_the_benchmark_net_of_cost_is_a_hit() -> None:
    r = score("BUY", *two("112", "210"))
    assert r == ScoreResult(
        "scored", D("12.0000"), D("5.0000"), D("0.25"), D("7.0000"), D("6.7500"), True, WEEK1
    )


def test_cost_can_flip_the_sign() -> None:
    r = score("BUY", *two("105.1", "210"))
    assert r is not None and r.excess == D("0.1000") and r.call_excess == D("-0.1500")
    assert r.hit is False
    acc = score("ACCUMULATE", *two("105.1", "210"))
    assert acc is not None and acc.call_excess == D("-0.1500") and acc.hit is False


def test_sell_is_a_hit_when_the_security_trails_the_benchmark() -> None:
    r = score("SELL", *two("97", "208"))
    assert r is not None and (r.ret, r.benchmark_ret) == (D("-3.0000"), D("4.0000"))
    assert r.call_excess == D("7.0000") and r.hit is True and r.cost_pct == D(0)
    assert r.excess == D("-7.0000")  # the raw sign is kept for display


def test_sell_is_a_miss_when_the_security_beats_the_benchmark() -> None:
    r = score("SELL", *two("104", "204"))
    assert r is not None and r.call_excess == D("-2.0000") and r.hit is False
    assert r.excess == D("2.0000")


@pytest.mark.parametrize("verdict", ["TRIM", "AVOID"])
def test_trim_and_avoid_follow_the_sell_rule(verdict: str) -> None:
    r = score(verdict, *two("97", "208"))
    assert r is not None and r.call_excess == D("7.0000") and r.hit is True


def test_hold_is_scored_but_has_no_hit_or_call_excess() -> None:
    r = score("HOLD", *two("112", "210"))
    assert r is not None and r.status == "scored" and r.excess == D("7.0000")
    assert r.hit is None and r.call_excess is None and r.cost_pct == D(0)


def test_insufficient_data_verdict_is_insufficient_whatever_the_bars() -> None:
    r = score("INSUFFICIENT_DATA", *two("112", "210"))
    assert r is not None and r.status == "insufficient_data" and r.ret is None
    early = score("INSUFFICIENT_DATA", *two("112", "210"), today=ISSUE)
    assert early is not None and early.status == "insufficient_data"


def test_pending_until_both_series_reach_the_target() -> None:
    sec = ser((ISSUE, 100), (date(2026, 1, 9), 110))
    bench = ser((ISSUE, 200), (WEEK1, 210))
    assert score("BUY", sec, bench, today=date(2026, 1, 13)) is None
    assert score("BUY", bench, sec, today=date(2026, 1, 13)) is None  # the other leg lags


def test_a_stale_leg_is_insufficient_only_after_the_grace_window() -> None:
    sec = ser((date(2025, 12, 12), 100), (ISSUE, 100))  # last bar 30+ days before the target
    bench = ser((ISSUE, 200), (WEEK1, 210), (date(2026, 1, 20), 211))
    assert score("BUY", sec, bench, today=date(2026, 1, 13)) is None
    r = score("BUY", sec, bench, today=date(2026, 1, 19))  # target + 7 days
    assert r is not None and r.status == "insufficient_data" and "no bar on or after" in r.reason


def test_a_bar_after_the_target_does_not_rescue_a_stale_end_bar() -> None:
    sec = ser((ISSUE, 100), (date(2026, 1, 30), 120))  # nothing between the issue day and the 30th
    bench = ser((ISSUE, 200), (date(2026, 1, 30), 210))
    ok = score("BUY", sec, bench, stale_days=7)  # the end bar is the issue day, 7 days back
    assert ok is not None and ok.status == "scored" and ok.ret == D("0.0000")
    r = score("BUY", sec, bench, stale_days=6)
    assert r is not None and r.status == "insufficient_data" and "stale" in r.reason
    assert score("BUY", sec, bench, stale_days=6, today=WEEK1) is None  # still inside the grace


def test_entry_bar_must_exist_and_be_close_enough() -> None:
    sec, bench = two("112", "210")
    old = score("BUY", sec, bench, price_date=None, as_of=date(2026, 1, 20), horizon_weeks=1)
    assert old is not None and old.status == "insufficient_data"  # 15 days from the last entry bar
    missing = score("BUY", sec, bench, price_date=date(2026, 1, 6), today=WEEK1)
    assert missing is None  # inside the grace window the call stays pending
    gone = score("BUY", sec, bench, price_date=date(2026, 1, 6))
    assert gone is not None and gone.status == "insufficient_data"


def test_weekend_issue_day_snaps_back_to_friday_without_a_price_date() -> None:
    fri, sat = date(2026, 1, 2), date(2026, 1, 3)
    sec = ser((fri, 100), (date(2026, 1, 9), 110), (date(2026, 1, 12), 111))
    bench = ser((fri, 200), (date(2026, 1, 9), 220), (date(2026, 1, 12), 221))
    r = score("BUY", sec, bench, as_of=sat, price_date=None)
    assert r is not None and r.ret == D("10.0000") and r.benchmark_ret == D("10.0000")
    assert r.end_date == date(2026, 1, 9)


def test_a_bar_dated_the_issue_day_is_ignored_when_price_date_is_the_prior_day() -> None:
    prior = date(2026, 1, 2)
    sec = ser((prior, 100), (ISSUE, 150), (WEEK1, 120))
    bench = ser((prior, 200), (ISSUE, 200), (WEEK1, 220))
    r = score("BUY", sec, bench, price_date=prior)
    assert r is not None and r.ret == D("20.0000")  # entry is 100, not the 150 bar of the issue day
    assert r.benchmark_ret == D("10.0000")  # benchmark entry: last bar on or before the entry bar


def test_benchmark_entry_snaps_back_within_five_days_or_is_insufficient() -> None:
    sec = ser((ISSUE, 100), (WEEK1, 110))
    ok = ser((date(2026, 1, 1), 200), (WEEK1, 220))
    r = score("BUY", sec, ok)
    assert r is not None and r.benchmark_ret == D("10.0000")
    far = ser((date(2025, 12, 20), 200), (WEEK1, 220))
    bad = score("BUY", sec, far)
    assert bad is not None and bad.status == "insufficient_data" and "benchmark" in bad.reason


def test_end_bar_snaps_back_over_a_holiday_and_is_never_after_the_target() -> None:
    sec = ser((ISSUE, 100), (date(2026, 1, 9), 110), (date(2026, 1, 13), 150))
    bench = ser((ISSUE, 200), (date(2026, 1, 9), 220), (date(2026, 1, 13), 300))
    r = score("BUY", sec, bench)  # target Mon 12th has no bar: Friday 9th is used
    assert r is not None and r.end_date == date(2026, 1, 9) and r.ret == D("10.0000")


def test_non_positive_entry_close_is_insufficient() -> None:
    r = score("BUY", *two("112", "210", sec_start="0"))
    assert r is not None and r.status == "insufficient_data" and "not positive" in r.reason
    r2 = score("BUY", *two("112", "210", bench_start="-1"))
    assert r2 is not None and r2.status == "insufficient_data"


def test_a_split_inside_one_adjusted_series_changes_nothing() -> None:
    # series arrive pre-adjusted, so a 4:1 split shows no jump: 100 -> 112 stays +12 percent
    r = score("BUY", *two("112", "210"))
    assert r is not None and r.ret == D("12.0000")


def test_deterministic_and_target_date() -> None:
    assert score("BUY", *two("112", "210")) == score("BUY", *two("112", "210"))
    assert target_date(ISSUE, 4) == date(2026, 2, 2) and target_date(ISSUE, 52) == date(2027, 1, 4)


def test_series_helpers() -> None:
    s = ser((ISSUE, 1), (WEEK1, 2))
    assert s.last_date() == WEEK1 and Series.of([]).last_date() is None
    assert s.at(ISSUE) == D(1) and s.at(date(2026, 1, 6)) is None
    assert s.on_or_before(date(2026, 1, 4)) is None
