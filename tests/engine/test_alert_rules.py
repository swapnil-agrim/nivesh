from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from nivesh_core.mf_models import FundMeta
from nivesh_core.schedule_config import AlertSettings
from nivesh_core.timeutil import IST
from nivesh_engine import dmath
from nivesh_engine.alert_rules import (
    Past,
    Pending,
    below_invalidation,
    big_move,
    break_52w,
    entered_zone,
    hygiene,
    mf_changes,
)
from nivesh_engine.bars import Bar
from nivesh_engine.calendar import Calendar
from nivesh_engine.ta import simple_returns

D = Decimal
MULT = D("2.5")


def series(today_move: Decimal, n: int = 22) -> list[Decimal]:
    """`n` closes: alternating plus and minus 10 percent days, then today's move."""
    out = [D(100)]
    for i in range(n - 2):
        out.append(out[-1] * (D("1.1") if i % 2 == 0 else D("0.9")))
    out.append(out[-1] * (1 + today_move))
    return out


def spread(closes: list[Decimal]) -> Decimal:
    base = simple_returns(closes[-22:-1])
    assert base is not None
    s = dmath.stdev(base)
    assert s is not None
    return s


def test_big_move_fires_above_two_and_a_half_times_the_baseline() -> None:
    s = spread(series(D(0)))
    assert big_move(series(D("2.6") * s), MULT) is not None
    assert big_move(series(D("2.4") * s), MULT) is None
    down = big_move(series(-D("2.6") * s), MULT)
    assert down is not None and down.rule == "big_move" and "-" in down.detail


def test_the_boundary_is_strict() -> None:
    s = spread(series(D(0)))
    edge = D("2.5") * s
    nudge = D("1E-20")
    assert big_move(series(edge - nudge), MULT) is None
    assert big_move(series(edge + nudge), MULT) is not None


def test_twenty_one_closes_are_not_enough_and_twenty_two_are() -> None:
    s = spread(series(D(0)))
    big = D("3") * s
    assert big_move(series(big, 21), MULT) is None
    assert big_move(series(big, 22), MULT) is not None
    assert big_move(series(big, 30), MULT) is not None  # only the last 22 count


def test_todays_move_is_left_out_of_its_own_baseline() -> None:
    quiet = series(D(0))
    s = spread(quiet)
    spike = series(D("3") * s)
    assert spread(spike) == s  # the baseline never saw the spike
    assert big_move(spike, MULT) is not None


def test_a_flat_baseline_gives_none() -> None:
    flat = [D(100)] * 21 + [D(110)]
    assert big_move(flat, MULT) is None
    assert big_move([D(0)] * 22, MULT) is None


def test_invalidation_needs_a_close_below_the_level() -> None:
    assert below_invalidation(D("100"), D("100")) is None
    assert below_invalidation(D("101"), D("100")) is None
    hit = below_invalidation(D("99.5"), D("100"))
    assert hit is not None and hit.rule == "close_below_invalidation" and "99.50" in hit.detail


def test_zone_entry_fires_once_on_entry() -> None:
    lo, hi = D(90), D(100)
    assert entered_zone(D(105), D(95), lo, hi) is not None
    assert entered_zone(D(95), D(96), lo, hi) is None  # staying inside
    assert entered_zone(D(80), D(105), lo, hi) is None  # jumped over it
    assert entered_zone(D(105), D(100), lo, hi) is not None  # edges are inside
    assert entered_zone(D(105), D(90), lo, hi) is not None
    assert entered_zone(D(105), D("89.99"), lo, hi) is None
    assert entered_zone(D(105), D(95), None, None) is None
    assert entered_zone(D(105), D(95), lo, None) is None
    assert entered_zone(D(95), D(105), lo, hi) is None  # leaving is not entering


def bars(n: int = 253, last_close: str = "100", last_volume: str = "1000") -> list[Bar]:
    start = date(2025, 1, 1)
    out = [
        Bar(start + timedelta(days=i), D(95), D(100) + D(i % 3), D(90), D(95), D(500))
        for i in range(n - 1)
    ]
    top = out[-1].date + timedelta(days=1)
    return [*out, Bar(top, None, None, None, D(last_close), D(last_volume))]


def test_52_week_break_needs_volume() -> None:
    # prior highs are 100..102, so a close of 103 is above the 252-session high
    assert break_52w(bars(last_close="103"), D("1.5")) is not None
    low_vol = break_52w(bars(last_close="103", last_volume="700"), D("1.5"))  # 1.4 x average
    assert low_vol is None
    edge = break_52w(bars(last_close="103", last_volume="750"), D("1.5"))  # exactly 1.5 x
    assert edge is None
    assert break_52w(bars(last_close="102"), D("1.5")) is None  # not above the high
    below = break_52w(bars(last_close="89"), D("1.5"))
    assert below is not None and "below" in below.detail and below.rule == "break_52w"


def test_52_week_break_needs_a_year_of_history_and_volume_data() -> None:
    assert break_52w(bars(n=252, last_close="103"), D("1.5")) is None
    none_vol = [*bars()[:-1], Bar(date(2026, 6, 1), None, None, None, D(103), None)]
    assert break_52w(none_vol, D("1.5")) is None
    thin = bars()
    thin[-21:-1] = [Bar(b.date, None, None, None, b.close, None) for b in thin[-21:-1]]
    assert break_52w(thin, D("1.5")) is None
    zero = bars()
    zero[-21:-1] = [Bar(b.date, None, None, None, b.close, D(0)) for b in zero[-21:-1]]
    assert break_52w(zero, D("1.5")) is None


def meta(**kw: object) -> FundMeta:
    base: dict[str, object] = dict(
        as_of=date(2026, 9, 1), amfi_code="100001", scheme_name="Example Fund",
        category="Large Cap", expense_ratio=D("1.00"), manager="A. Person", source="amfi",
    )  # fmt: skip
    return FundMeta.model_validate({**base, **kw})


def rules(prev: FundMeta | None, cur: FundMeta) -> list[str]:
    return [h.rule for h in mf_changes(prev, cur, D("0.2"))]


def test_fund_changes() -> None:
    old = meta()
    assert rules(old, meta(as_of=date(2026, 10, 1))) == []
    assert rules(None, meta()) == []
    assert rules(old, meta(manager="B. Person")) == ["mf_manager_change"]
    assert rules(old, meta(category="Mid Cap")) == ["mf_category_change"]
    assert rules(old, meta(expense_ratio=D("1.20"))) == []  # exactly 0.2 points
    assert rules(old, meta(expense_ratio=D("1.21"))) == ["mf_ter_change"]
    assert rules(old, meta(expense_ratio=D("0.70"))) == ["mf_ter_change"]  # a fall counts too
    assert rules(old, meta(manager=None, category=None, expense_ratio=None)) == []


def test_merger_is_a_labelled_proxy() -> None:
    old = meta()
    by_code = mf_changes(old, meta(amfi_code="100002"), D("0.2"))
    assert [h.rule for h in by_code] == ["mf_merger"] and "best effort" in by_code[0].detail
    assert rules(old, meta(scheme_name="Example Fund (Merged)")) == ["mf_merger"]
    assert rules(old, meta(source="notice: scheme merger")) == ["mf_merger"]
    assert rules(old, meta(scheme_name="Emerging Markets Fund")) == []  # not a merger


# ---- hygiene -------------------------------------------------------------------------------------
CALS = {"IN": Calendar.nse({2026: []}), "US": Calendar.nyse()}
CFG = AlertSettings()


def at(day: date, hh: int = 17, mm: int = 30) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


def pend(
    rule: str = "big_move", sid: int | None = 1, day: date = date(2026, 10, 16), m: str = "IN"
):  # type: ignore[no-untyped-def]
    return Pending(rule, sid, m, day)


def past(day: date, rule: str = "big_move", sid: int | None = 1, hh: int = 17) -> Past:
    return Past(rule, sid, day, at(day, hh))


def test_same_rule_and_security_is_held_for_five_sessions() -> None:
    fri = date(2026, 10, 9)
    hist = [past(fri)]
    for day, held in [(date(2026, 10, 12), True), (date(2026, 10, 16), True),
                      (date(2026, 10, 19), False)]:  # fmt: skip
        plan = hygiene([pend(day=day)], hist, CALS, at(day), CFG)
        assert plan.held == (((0, "dedupe"),) if held else ()), day
        assert plan.send == (() if held else (0,))


def test_other_rule_or_security_is_not_a_repeat() -> None:
    hist = [past(date(2026, 10, 15))]
    fired = [pend("break_52w"), pend(sid=2), pend(sid=None)]
    plan = hygiene(fired, hist, CALS, at(date(2026, 10, 16)), AlertSettings(digest_threshold=9))
    assert plan.send == (0, 1, 2) and plan.held == ()


def test_weekends_and_holidays_are_not_sessions() -> None:
    cals = {"IN": Calendar.nse({2026: [date(2026, 10, 13)]}), "US": Calendar.nyse()}
    hist = [past(date(2026, 10, 9))]
    # Oct 12, 14, 15, 16, 19 are the five sessions after the 9th (13th is a listed holiday)
    held = hygiene([pend(day=date(2026, 10, 19))], hist, cals, at(date(2026, 10, 19)), CFG)
    assert held.held == ((0, "dedupe"),)
    free = hygiene([pend(day=date(2026, 10, 20))], hist, cals, at(date(2026, 10, 20)), CFG)
    assert free.send == (0,)


def test_each_security_uses_its_own_market_sessions() -> None:
    # the US market is shut for Thanksgiving on the 26th; the Indian one is open that day
    hist = [Past("big_move", 1, date(2026, 11, 20), at(date(2026, 11, 20)))]
    day = date(2026, 11, 30)
    us = hygiene([pend(day=day, m="US")], hist, CALS, at(day), CFG)
    india = hygiene([pend(day=day, m="IN")], hist, CALS, at(day), CFG)
    assert us.held == ((0, "dedupe"),) and india.send == (0,)


def test_an_unknown_year_falls_back_to_weekdays_with_a_note() -> None:
    hist = [Past("big_move", 1, date(2026, 12, 30), at(date(2026, 12, 30)))]
    early = hygiene([pend(day=date(2027, 1, 4))], hist, CALS, at(date(2027, 1, 4)), CFG)
    assert early.held == ((0, "dedupe"),) and early.notes == ((0, "weekday fallback"),)
    late = hygiene([pend(day=date(2027, 1, 12))], hist, CALS, at(date(2027, 1, 12)), CFG)
    assert late.send == (0,) and late.notes == ((0, "weekday fallback"),)


def test_a_repeat_inside_one_batch_is_held() -> None:
    plan = hygiene([pend(), pend()], [], CALS, at(date(2026, 10, 16)), CFG)
    assert plan.send == (0,) and plan.held == ((1, "dedupe"),)


def test_four_in_an_hour_make_one_digest_and_three_do_not() -> None:
    day = date(2026, 10, 16)
    four = [pend(sid=i) for i in range(4)]
    assert hygiene(four, [], CALS, at(day), CFG).digest == (0, 1, 2, 3)
    three = hygiene(four[:3], [], CALS, at(day), CFG)
    assert three.send == (0, 1, 2) and three.digest == ()
    two = hygiene(four[:2], [past(day, sid=9, hh=17), past(day, sid=8, hh=17)], CALS, at(day), CFG)
    assert two.digest == (0, 1)  # two already went out inside the hour
    old = [past(day, sid=9, hh=15), past(day, sid=8, hh=15)]
    assert hygiene(four[:2], old, CALS, at(day), CFG).send == (0, 1)  # two hours ago: not counted
    one = hygiene(four[:1], [past(day, sid=i + 20) for i in range(5)], CALS, at(day), CFG)
    assert one.send == (0,)  # a single alert is never a digest


def test_daily_cap_holds_back_the_ninth() -> None:
    day = date(2026, 10, 16)
    cfg = AlertSettings(digest_threshold=99)
    nine = [pend(sid=i) for i in range(9)]
    plan = hygiene(nine, [], CALS, at(day), cfg)
    assert plan.send == tuple(range(8)) and plan.held == ((8, "cap"),)
    used = [past(day, sid=100 + i) for i in range(7)]
    again = hygiene(nine[:3], used, CALS, at(day), cfg)
    assert again.send == (0,) and again.held == ((1, "cap"), (2, "cap"))


def test_the_cap_resets_at_midnight_in_india() -> None:
    cfg = AlertSettings(digest_threshold=99)
    late = Past("big_move", 5, date(2026, 10, 15), datetime(2026, 10, 15, 23, 59, tzinfo=IST))
    history = [late] * 8
    before = hygiene(
        [pend(sid=1)], history, CALS, datetime(2026, 10, 15, 23, 59, 30, tzinfo=IST), cfg
    )
    after = hygiene([pend(sid=1)], history, CALS, datetime(2026, 10, 16, 0, 1, tzinfo=IST), cfg)
    assert before.held == ((0, "cap"),) and after.send == (0,)
    utc_view = datetime(2026, 10, 15, 18, 31, tzinfo=UTC)  # 00:01 IST on the 16th
    assert hygiene([pend(sid=1)], history, CALS, utc_view, cfg).send == (0,)


def test_a_held_alert_does_not_extend_the_window() -> None:
    """The history holds sent alerts only: after a held repeat, the window still counts from the
    last alert that went out."""
    fri = date(2026, 10, 9)
    hist = [past(fri)]
    held = hygiene([pend(day=date(2026, 10, 12))], hist, CALS, at(date(2026, 10, 12)), CFG)
    assert held.held == ((0, "dedupe"),)
    sixth = hygiene([pend(day=date(2026, 10, 19))], hist, CALS, at(date(2026, 10, 19)), CFG)
    assert sixth.send == (0,)


def test_hygiene_is_deterministic() -> None:
    day = date(2026, 10, 16)
    args = ([pend(sid=i) for i in range(5)], [past(day, sid=50)], CALS, at(day), CFG)
    assert hygiene(*args) == hygiene(*args)


@pytest.mark.parametrize("closes", [[D(1)] * 3, []])
def test_short_series_are_none(closes: list[Decimal]) -> None:
    assert big_move(closes, MULT) is None
