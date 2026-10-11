"""The scheduler tick (ST-11.1): run what is due, once, with retries and a failure notice.

One call to `run_due` is idempotent. A job belongs to a (job, trading date) pair that is claimed
in the transactional store before the job runs; a second tick, or an overlapping one, finds the
claim and leaves the job alone. A failing job is retried with the configured waits (NFR-12); on
final failure one notice goes out through the injected `notifier`, which is the only egress.
Jobs are plain functions, never separate programs, and nothing here installs anything.
"""

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from nivesh_adapters.delivery import Message, Outcome, plain_message
from nivesh_adapters.market_ingest import calendar_for
from nivesh_adapters.run_support import BudgetRefused
from nivesh_adapters.stores import IN_USE
from nivesh_core.alerts import (
    claim_job,
    finish_job,
    finished_jobs,
    release_job,
)
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import CalendarUnknown, NiveshError
from nivesh_core.profile import Profile
from nivesh_core.redact import redact_text
from nivesh_core.schedule_config import JobSpec, ScheduleSettings
from nivesh_core.timeutil import IST
from nivesh_engine.calendar import Calendar
from nivesh_engine.schedule import MISSED, Due, backoff_plan, due_jobs

Notifier = Callable[[Message], list[Outcome]]
Sleep = Callable[[float], None]
LOOKBACK_DAYS = 5  # how far back finished rows are read to match against due jobs
REASON_CHARS = 200
NO_CHANNEL = "no channel configured"
PATH = re.compile(r"(?:[A-Za-z]:)?[/\\][^\s]+")


@dataclass(frozen=True)
class JobCtx:
    settings: Settings
    profile: Profile
    sched: ScheduleSettings
    spec: JobSpec
    day: date  # the trading date
    now: datetime  # the tick instant, aware UTC
    clock: Callable[[], datetime]
    market: str


@dataclass(frozen=True)
class JobResult:
    status: Literal["ok", "skipped"]
    summary: str = ""
    run_id: int | None = None
    final: bool = True  # False: a skip that a later tick inside the grace window may redo


@dataclass(frozen=True)
class JobReport:
    job: str
    trading_date: date
    status: str  # ok | failed | skipped | waiting | due | already running
    attempts: int = 0
    reason: str = ""
    late: bool = False
    notified: bool = False


Registry = dict[str, Callable[[JobCtx], JobResult]]


def failure_message(job: str, day: date, reason: str, at_utc: datetime) -> Message:
    """The failure notice: the job, its trading date, the IST time and a curated reason."""
    stamp = at_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    text = f"{job} for {day.isoformat()} did not complete ({stamp} IST).\nReason: {reason}"
    return plain_message(f"nivesh job failed: {job}", text)


def describe(e: Exception) -> str:
    """A short reason that is safe to store and send: the type, and for expected failures the
    redacted text without paths or long digit runs."""
    if isinstance(e, BudgetRefused):
        return "budget cap: " + _clean(str(e))
    if isinstance(e, NiveshError):
        if IN_USE in str(e):
            return "market database in use"
        return f"{type(e).__name__}: {_clean(str(e))}"
    return type(e).__name__


def _clean(text: str) -> str:
    return redact_text(PATH.sub("<path>", " ".join(text.split())))[:REASON_CHARS]


def _notify(notifier: Notifier, msg: Message) -> tuple[bool, str]:
    """(delivered, why not). A notifier that raises is a failed notice, never a crash."""
    try:
        outcomes = notifier(msg)
    except Exception:  # the text of a transport error can carry an address
        return False, "notice failed"
    if any(o.status == "sent" for o in outcomes):
        return True, ""
    return False, "notice not delivered" if outcomes else NO_CHANNEL


def _already_told(conn: sqlite3.Connection, reason: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM job_run WHERE reason LIKE ? AND notified = 1 LIMIT 1", (reason + "%",)
    ).fetchone()
    return row is not None


def calendars_for(settings: Settings) -> dict[str, Calendar]:
    return {"IN": calendar_for("IN", settings), "US": calendar_for("US", settings)}


def run_due(
    settings: Settings,
    profile: Profile,
    schedule: ScheduleSettings,
    registry: Registry,
    at_utc: datetime,
    notifier: Notifier,
    sleep: Sleep,
    *,
    dry_run: bool = False,
    calendars: Mapping[str, Calendar] | None = None,
) -> list[JobReport]:
    """Run every job that is due at the aware instant `at_utc`. With `dry_run` nothing is
    written, run or sent: the due jobs are only listed."""
    cals = calendars or calendars_for(settings)
    data_dir = Path(settings.data_dir)
    path = data_dir / "nivesh.sqlite"
    if dry_run and not path.exists():
        return _plan(due_jobs(schedule.jobs, cals, at_utc, set(), schedule.us_lead_min))
    init_stores(data_dir)
    conn = open_sqlite(path)
    try:
        since = at_utc.astimezone(IST).date() - timedelta(days=LOOKBACK_DAYS)
        done = finished_jobs(conn, since)
        due = due_jobs(schedule.jobs, cals, at_utc, done, schedule.us_lead_min)
        if dry_run:
            return _plan(due)
        reports: list[JobReport] = []
        for d in due:
            rep = _run_one(conn, d, settings, profile, schedule, registry, at_utc, notifier, sleep)
            if rep is not None:
                reports.append(rep)
        return reports
    finally:
        conn.close()


def _plan(due: list[Due]) -> list[JobReport]:
    return [
        JobReport(d.job.name, d.trading_date, "skip" if d.skip_reason else "due",
                  reason=d.skip_reason or "", late=d.late)
        for d in due
    ]  # fmt: skip


def _run_one(
    conn: sqlite3.Connection, d: Due, settings: Settings, profile: Profile, sched: ScheduleSettings,
    registry: Registry, at_utc: datetime, notifier: Notifier, sleep: Sleep,
) -> JobReport | None:  # fmt: skip
    job, day = d.job, d.trading_date
    if not claim_job(conn, job.name, day, at_utc, job.stale_minutes):
        return None
    if d.skip_reason is not None:
        return _skip(conn, d, at_utc, notifier)
    fn = registry.get(job.name)
    if fn is None:
        return _fail(conn, d, 0, "no such job registered", at_utc, notifier)
    ctx = JobCtx(settings, profile, sched, job, day, at_utc, lambda: at_utc, job.market)
    waits = backoff_plan(sched.retries, sched.backoff_s)
    reason = ""
    for attempt in range(1, sched.retries + 2):
        try:
            result = fn(ctx)
        except (BudgetRefused, CalendarUnknown) as e:  # retrying cannot help
            return _fail(conn, d, attempt, describe(e), at_utc, notifier)
        except Exception as e:  # everything else may be a passing fault
            reason = describe(e)
            if attempt <= len(waits):
                sleep(waits[attempt - 1])
            continue
        return _finish_ok(conn, d, attempt, result, at_utc)
    return _fail(conn, d, sched.retries + 1, reason, at_utc, notifier)


def _finish_ok(
    conn: sqlite3.Connection, d: Due, attempts: int, r: JobResult, at_utc: datetime
) -> JobReport:
    job, day = d.job, d.trading_date
    if (
        r.status == "skipped"
        and not r.final
        and at_utc <= d.instant_utc + timedelta(minutes=job.grace_minutes)
    ):
        release_job(conn, job.name, day)  # a later tick may redo it
        return JobReport(job.name, day, "waiting", attempts, r.summary, d.late)
    finish_job(
        conn, job.name, day, r.status, attempts, r.run_id, r.summary, d.late, False, at_utc
    )  # fmt: skip
    return JobReport(job.name, day, r.status, attempts, r.summary, d.late)


def _fail(
    conn: sqlite3.Connection,
    d: Due,
    attempts: int,
    reason: str,
    at_utc: datetime,
    notifier: Notifier,
) -> JobReport:
    job, day = d.job, d.trading_date
    sent, why = _notify(notifier, failure_message(job.name, day, reason, at_utc))
    note = reason if sent else f"{reason}; {why}"
    finish_job(conn, job.name, day, "failed", attempts, None, note, d.late, sent, at_utc)
    return JobReport(job.name, day, "failed", attempts, note, d.late, sent)


def _skip(conn: sqlite3.Connection, d: Due, at_utc: datetime, notifier: Notifier) -> JobReport:
    """A job that cannot run on this date. A missed window is only recorded; an unknown holiday
    list is reported once, however many jobs and days it blocks."""
    job, day, reason = d.job, d.trading_date, d.skip_reason or ""
    sent = False
    if reason != MISSED and not _already_told(conn, reason):
        sent, why = _notify(notifier, failure_message(job.name, day, reason, at_utc))
        note = reason if sent else f"{reason}; {why}"
    else:
        note = reason
    finish_job(conn, job.name, day, "skipped", 0, None, note, d.late, sent, at_utc)
    return JobReport(job.name, day, "skipped", 0, note, d.late, sent)
