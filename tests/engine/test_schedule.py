from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nivesh_core.schedule_config import JobSpec, ScheduleSettings
from nivesh_core.timeutil import IST
from nivesh_engine.calendar import Calendar
from nivesh_engine.schedule import (
    backoff_plan,
    classify_late,
    due_jobs,
    instant_for,
    is_first_weekday_of_month,
    is_stale,
    next_instant,
    us_is_dst,
    us_preopen_utc,
)

JOBS = {j.name: j for j in ScheduleSettings().jobs}
ENABLED = [JOBS[n].model_copy(update={"enabled": True}) for n in JOBS]
HOLIDAYS = {2026: [date(2026, 10, 2)]}
CALS = {"IN": Calendar.nse(HOLIDAYS), "US": Calendar.nyse()}


def ist(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(UTC)


def due(at: datetime, finished: set[tuple[str, date]] | None = None, jobs=None):  # type: ignore[no-untyped-def]
    return due_jobs(jobs or ENABLED, CALS, at, finished or set())


def names(rows) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(f"{r.job.name}@{r.trading_date}" for r in rows if r.skip_reason is None)


def test_india_brief_is_due_at_eight_and_not_a_minute_before() -> None:
    assert "brief_in@2026-10-09" not in names(due(ist(2026, 10, 9, 7, 59)))
    rows = [r for r in due(ist(2026, 10, 9, 8, 0)) if r.job.name == "brief_in"]
    assert [(r.trading_date, r.late, r.skip_reason) for r in rows if r.skip_reason is None] == [
        (date(2026, 10, 9), False, None)
    ]
    assert rows[-1].instant_utc == ist(2026, 10, 9, 8, 0)


def test_holiday_and_weekend_have_no_trading_day_jobs() -> None:
    holiday = names(due(ist(2026, 10, 2, 9)))  # listed NSE holiday (Friday)
    assert not [n for n in holiday if n.endswith("2026-10-02")]
    weekend = names(due(ist(2026, 10, 10, 9, 30)))  # Saturday
    assert not [n for n in weekend if "2026-10-10" in n and n.startswith(("brief_in", "login"))]
    assert not [n for n in weekend if n.startswith("monitors@2026-10-10")]
    assert {"rescore@2026-10-10", "weekly_review@2026-10-10", "backup@2026-10-10"} <= set(weekend)


def test_only_the_first_saturday_runs_the_monthly_review() -> None:
    assert "monthly_fund_review@2026-11-07" in names(due(ist(2026, 11, 7, 9, 1)))
    assert "monthly_fund_review@2026-11-14" not in names(due(ist(2026, 11, 14, 9, 1)))
    assert "weekly_review@2026-11-14" in names(due(ist(2026, 11, 14, 9, 1)))
    assert is_first_weekday_of_month(date(2026, 11, 7)) and not is_first_weekday_of_month(
        date(2026, 11, 14)
    )


@pytest.mark.parametrize(
    ("day", "dst"),
    [
        (date(2026, 3, 7), False), (date(2026, 3, 8), True), (date(2026, 3, 9), True),
        (date(2026, 10, 31), True), (date(2026, 11, 1), False), (date(2026, 11, 2), False),
        (date(2027, 3, 13), False), (date(2027, 3, 14), True), (date(2027, 11, 1), True),
        (date(2027, 11, 6), True), (date(2027, 11, 7), False),
    ],
)  # fmt: skip
def test_us_dst_is_computed_by_rule(day: date, dst: bool) -> None:
    assert us_is_dst(day) is dst


def test_us_dst_refuses_years_before_the_current_rule() -> None:
    with pytest.raises(ValueError):
        us_is_dst(date(2006, 6, 1))


def test_us_preopen_is_half_an_hour_before_the_open_in_both_seasons() -> None:
    edt = us_preopen_utc(date(2026, 10, 30), 30).astimezone(IST)
    est = us_preopen_utc(date(2026, 11, 2), 30).astimezone(IST)
    assert (edt.hour, edt.minute) == (18, 30) and (est.hour, est.minute) == (19, 30)
    assert edt.date() == date(2026, 10, 30) and est.date() == date(2026, 11, 2)
    assert us_preopen_utc(date(2026, 3, 6), 30).astimezone(IST).hour == 19  # EST, before the switch
    assert us_preopen_utc(date(2026, 3, 9), 30).astimezone(IST).hour == 18  # EDT


def test_brief_us_follows_the_dst_shift_through_due_jobs() -> None:
    def hit(at: datetime) -> list[str]:
        return [n for n in names(due(at)) if n.startswith("brief_us")]

    assert hit(datetime(2026, 10, 30, 12, 59, tzinfo=UTC)) == []
    assert hit(datetime(2026, 10, 30, 13, 0, tzinfo=UTC)) == ["brief_us@2026-10-30"]  # 18:30 IST
    assert hit(datetime(2026, 11, 2, 13, 0, tzinfo=UTC)) == []  # EST: not yet
    assert hit(datetime(2026, 11, 2, 14, 0, tzinfo=UTC)) == ["brief_us@2026-11-02"]  # 19:30 IST


def test_us_holidays_are_skipped() -> None:
    juneteenth = datetime(2026, 6, 19, 14, 30, tzinfo=UTC)
    good_friday = datetime(2026, 4, 3, 14, 30, tzinfo=UTC)
    for at in (juneteenth, good_friday):
        assert not [n for n in names(due(at)) if n.startswith("brief_us")]


def test_us_monitors_run_the_morning_after_the_session() -> None:
    def hit(at: datetime) -> list[str]:
        return [n for n in names(due(at)) if n.startswith("monitors_us")]

    # Thursday session 2026-10-29 is covered at Friday 05:30 IST (EDT or EST alike)
    assert hit(ist(2026, 10, 30, 5, 29)) == []
    assert hit(ist(2026, 10, 30, 5, 30)) == ["monitors_us@2026-10-29"]
    # Friday's session is covered on Saturday morning, not on Monday
    assert hit(ist(2026, 10, 10, 5, 30)) == ["monitors_us@2026-10-09"]
    assert hit(ist(2026, 10, 12, 5, 30)) == []  # Monday morning: Friday's window is long gone
    # Monday's session is covered on Tuesday morning; Sunday morning covers nothing
    assert hit(ist(2026, 10, 13, 5, 30)) == ["monitors_us@2026-10-12"]
    assert hit(ist(2026, 10, 11, 5, 30)) == []
    # across the November switch the IST time of the job does not move
    assert hit(ist(2026, 11, 3, 5, 30)) == ["monitors_us@2026-11-02"]


def test_ist_midnight_boundary_picks_the_right_trading_date() -> None:
    early = JobSpec(name="early", market="IN", at="01:30")
    at = datetime(2026, 10, 8, 20, 30, tzinfo=UTC)  # 02:00 IST on the 9th, still the 8th in UTC
    got = due_jobs([early], CALS, at, set())
    assert [(r.trading_date, r.skip_reason) for r in got] == [(date(2026, 10, 9), None)]
    assert due_jobs([early], CALS, at, {("early", date(2026, 10, 9))}) == []
    before = datetime(2026, 10, 8, 19, 59, tzinfo=UTC)  # 01:29 IST
    assert [r.trading_date for r in due_jobs([early], CALS, before, set())] == []
    # an earlier date's missed window is not reported again the next morning
    assert not [r for r in due(ist(2026, 10, 9, 5, 0)) if r.job.name == "brief_in"]


def test_late_inside_grace_after_grace_and_past_catchup() -> None:
    def brief(at: datetime):  # type: ignore[no-untyped-def]
        return [r for r in due(at) if r.job.name == "brief_in" and r.trading_date.day == 9][0]

    assert not brief(ist(2026, 10, 9, 8, 30)).late
    late = brief(ist(2026, 10, 9, 8, 31))
    assert late.late and late.skip_reason is None
    assert brief(ist(2026, 10, 9, 11, 0)).skip_reason is None  # exactly catchup_hours (3)
    missed = brief(ist(2026, 10, 9, 22, 0))  # the 22:00 laptop wake-up must not send a brief
    assert missed.skip_reason == "missed window" and missed.late


def test_unknown_calendar_year_is_a_skip_reason_not_a_guess() -> None:
    rows = [r for r in due(ist(2027, 1, 4, 8, 5)) if r.job.name == "brief_in"]
    assert len(rows) == 1 and rows[0].skip_reason is not None
    assert "2027" in rows[0].skip_reason and "incomplete" in rows[0].skip_reason
    assert rows[0].trading_date == date(2027, 1, 4)


def test_finished_and_disabled_jobs_are_not_due() -> None:
    at = ist(2026, 10, 9, 8, 5)
    assert "brief_in@2026-10-09" in names(due(at))
    assert "brief_in@2026-10-09" not in names(due(at, {("brief_in", date(2026, 10, 9))}))
    default = [j for j in ScheduleSettings().jobs]
    assert not [r for r in due_jobs(default, CALS, datetime(2026, 10, 30, 13, 0, tzinfo=UTC), set())
                if r.job.name == "brief_us"]  # fmt: skip


def test_instant_for_and_helpers() -> None:
    spec = JobSpec(name="x", at="09:15", market="none", trading_days_only=False)
    assert instant_for(spec, date(2026, 10, 9), 30) == ist(2026, 10, 9, 9, 15)
    assert backoff_plan(2, (30, 120)) == [30, 120] and backoff_plan(0, ()) == []
    with pytest.raises(ValueError):
        backoff_plan(2, (30,))
    at, inst = datetime(2026, 1, 1, 8, 0, tzinfo=UTC), datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
    assert classify_late(at, inst, 60, 2) == "on_time"
    assert classify_late(at + timedelta(minutes=1), inst, 60, 2) == "late"
    assert classify_late(at + timedelta(hours=1, minutes=1), inst, 60, 2) == "missed"
    assert is_stale(None, date(2026, 10, 9))
    assert is_stale(date(2026, 10, 8), date(2026, 10, 9))
    assert not is_stale(date(2026, 10, 9), date(2026, 10, 9))
    assert not is_stale(date(2026, 10, 10), date(2026, 10, 9))


def test_engine_source_has_no_tz_database() -> None:
    src = (Path(__file__).resolve().parents[2] / "nivesh_engine" / "schedule.py").read_text()
    assert "zoneinfo" not in src and "tzdata" not in src


def test_next_instant_skips_weekends_holidays_and_unknown_years() -> None:
    brief = JOBS["brief_in"]
    fri = ist(2026, 10, 9, 8, 0)
    assert next_instant(brief, CALS, fri) == ist(2026, 10, 12, 8, 0)  # Monday
    assert next_instant(brief, CALS, ist(2026, 10, 1, 9)) == ist(2026, 10, 5, 8, 0)  # 2nd is listed
    assert next_instant(brief, CALS, ist(2026, 12, 31, 9)) is None  # 2027 has no NSE list
    assert next_instant(JOBS["rescore"], CALS, fri) == ist(2026, 10, 10, 8, 30)
    assert next_instant(JOBS["monthly_fund_review"], CALS, fri) == ist(2026, 11, 7, 9, 0)
    assert next_instant(JOBS["monitors_us"], CALS, ist(2026, 10, 9, 6)) == ist(2026, 10, 10, 5, 30)
    assert next_instant(JOBS["backup"], CALS, fri, horizon_days=0) is None  # nothing that close


def test_us_monitors_across_the_march_switch_and_a_monday_session() -> None:
    def hit(at: datetime) -> list[str]:
        return [n for n in names(due(at)) if n.startswith("monitors_us")]

    # Friday 6 March (EST) is covered on Saturday morning; the clocks change on Sunday the 8th
    assert hit(ist(2026, 3, 7, 5, 30)) == ["monitors_us@2026-03-06"]
    assert hit(ist(2026, 3, 8, 5, 30)) == []  # Saturday had no session
    # Monday 9 March (EDT) is covered on Tuesday at the same IST time
    assert hit(ist(2026, 3, 10, 5, 30)) == ["monitors_us@2026-03-09"]
    # IST date differs from the session date for every run of this job
    rows = [r for r in due(ist(2026, 3, 10, 5, 30)) if r.job.name == "monitors_us"]
    assert rows[-1].trading_date == date(2026, 3, 9)
    assert rows[-1].instant_utc.astimezone(IST).date() == date(2026, 3, 10)
