"""When a job is due (ST-11.1). Pure: calendars and the instant come in as arguments.

A job belongs to a trading date: the market-local session date for a market job, the IST date
otherwise. It is due once its instant has passed and no finished record exists for the pair.
The US pre-open instant is computed by rule (US summer time runs from the second Sunday of March
to the first Sunday of November), so no time-zone database is needed.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from nivesh_core.errors import CalendarUnknown
from nivesh_core.schedule_config import JobSpec
from nivesh_core.timeutil import IST, ist_date
from nivesh_engine.calendar import Calendar

MISSED = "missed window"
Lateness = Literal["on_time", "late", "missed"]


@dataclass(frozen=True)
class Due:
    job: JobSpec
    trading_date: date
    instant_utc: datetime
    late: bool
    skip_reason: str | None


def _sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def us_is_dst(day: date) -> bool:
    """US summer time applies to the 09:30 open of `day` (current rule, years from 2007)."""
    if day.year < 2007:
        raise ValueError("US summer-time rule is implemented from 2007")
    return _sunday(day.year, 3, 2) <= day < _sunday(day.year, 11, 1)


def us_preopen_utc(day: date, lead_min: int) -> datetime:
    """`lead_min` minutes before the 09:30 New York open of session `day`, as UTC."""
    open_utc = datetime.combine(day, time(13 if us_is_dst(day) else 14, 30), tzinfo=UTC)
    return open_utc - timedelta(minutes=lead_min)


def is_first_weekday_of_month(day: date) -> bool:
    return day.day <= 7


def instant_for(job: JobSpec, day: date, us_lead_min: int) -> datetime:
    """When `job` runs for trading date `day`, as UTC."""
    if job.market == "US" and job.day_offset == 0:
        return us_preopen_utc(day, us_lead_min)
    hh, mm = int(job.at[:2]), int(job.at[3:])
    local = datetime.combine(day + timedelta(days=job.day_offset), time(hh, mm), tzinfo=IST)
    return local.astimezone(UTC)


def backoff_plan(retries: int, backoff_s: tuple[int, ...]) -> list[int]:
    """The wait in seconds before each retry."""
    if len(backoff_s) != retries:
        raise ValueError("one wait per retry is needed")
    return list(backoff_s)


def classify_late(
    at_utc: datetime, instant: datetime, grace_minutes: int, catchup_hours: int
) -> Lateness:
    age = at_utc - instant
    if age > timedelta(hours=catchup_hours):
        return "missed"
    return "late" if age > timedelta(minutes=grace_minutes) else "on_time"


def is_stale(latest: date | None, expected: date) -> bool:
    """Stored data is stale when its newest date is older than the session it should cover."""
    return latest is None or latest < expected


def _matches(job: JobSpec, day: date) -> bool:
    if job.weekday is not None and day.weekday() != job.weekday:
        return False
    return job.nth_weekday is None or is_first_weekday_of_month(day)


def due_jobs(
    jobs: Collection[JobSpec],
    calendars: Mapping[str, Calendar],
    at_utc: datetime,
    finished: Collection[tuple[str, date]],
    us_lead_min: int = 30,
) -> list[Due]:
    """Enabled jobs whose instant has passed with no finished record, oldest first. A job past
    its catch-up window on the same IST date is returned with the reason "missed window"; a
    market whose holiday list is unknown for the year is returned with that reason, never
    guessed."""
    today = ist_date(at_utc)  # rejects a naive instant
    out: list[Due] = []
    for job in jobs:
        if not job.enabled:
            continue
        catchup = timedelta(hours=job.catchup_hours)
        for k in range(catchup.days + 2 + job.day_offset, -1, -1):
            day = today - timedelta(days=k)
            if (job.name, day) in finished or not _matches(job, day):
                continue
            instant = instant_for(job, day, us_lead_min)
            if instant > at_utc:
                continue
            if at_utc - instant > catchup and ist_date(instant) != today:
                continue  # a window missed on an earlier date is not recorded again
            reason: str | None = None
            if job.trading_days_only:
                try:
                    if not calendars[job.market].is_session(day):
                        continue
                except CalendarUnknown:
                    name = calendars[job.market].market
                    reason = f"{name} holiday list for {day.year} incomplete, owner action"
            lateness = classify_late(at_utc, instant, job.grace_minutes, job.catchup_hours)
            if reason is None and lateness == "missed":
                reason = MISSED
            out.append(Due(job, day, instant, lateness != "on_time", reason))
    return sorted(out, key=lambda d: (d.instant_utc, d.job.name))


def next_instant(
    job: JobSpec,
    calendars: Mapping[str, Calendar],
    after_utc: datetime,
    us_lead_min: int = 30,
    horizon_days: int = 40,
) -> datetime | None:
    """The first instant after `after_utc` at which `job` is due, within `horizon_days`; None
    when there is none or a holiday list needed for it is unknown."""
    start = ist_date(after_utc) - timedelta(days=2)
    for k in range(horizon_days + 3):
        day = start + timedelta(days=k)
        if not _matches(job, day):
            continue
        instant = instant_for(job, day, us_lead_min)
        if instant <= after_utc:
            continue
        try:
            if job.trading_days_only and not calendars[job.market].is_session(day):
                continue
        except CalendarUnknown:
            continue
        return instant
    return None
