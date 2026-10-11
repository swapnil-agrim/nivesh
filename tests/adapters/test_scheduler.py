import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nivesh_adapters.delivery import Message, Outcome
from nivesh_adapters.run_support import BudgetRefused, plain_run
from nivesh_adapters.scheduler import (
    JobCtx,
    JobReport,
    JobResult,
    Registry,
    describe,
    failure_message,
    run_due,
)
from nivesh_core.alerts import claim_job
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.errors import CalendarUnknown, NiveshError
from nivesh_core.pii_scan import scan_text
from nivesh_core.profile import Profile, load_profile
from nivesh_core.schedule_config import JobSpec, ScheduleSettings
from nivesh_engine.calendar import Calendar

Env = tuple[list[str], Path]
CALS = {"IN": Calendar.nse({2026: [date(2026, 10, 2)]}), "US": Calendar.nyse()}
EIGHT = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)  # Friday 08:00 IST
DAY = date(2026, 10, 9)


def spec(name: str = "t1", **kw: object) -> JobSpec:
    return JobSpec.model_validate({"name": name, "market": "IN", "at": "08:00", **kw})


def sched(*jobs: JobSpec, **kw: object) -> ScheduleSettings:
    return ScheduleSettings.model_validate({"jobs": [j.model_dump() for j in jobs], **kw})


class Tick:
    """One scheduler setup: collector notifier, sleep recorder, call counter."""

    def __init__(self, env: Env) -> None:
        cfg = Path(env[0][1])
        self.settings: Settings = load_settings(cfg / "nivesh.yaml")
        self.profile: Profile = load_profile(cfg / "profile.yaml")
        self.data = env[1]
        self.sent: list[Message] = []
        self.sleeps: list[float] = []
        self.outcomes: list[Outcome] = [Outcome("telegram", "sent", 1)]
        self.calls = 0

    def notifier(self, msg: Message) -> list[Outcome]:
        self.sent.append(msg)
        return list(self.outcomes)

    def run(
        self, registry: Registry, schedule: ScheduleSettings, at: datetime = EIGHT, **kw: object
    ) -> list[JobReport]:
        return run_due(
            self.settings, self.profile, schedule, registry, at, self.notifier,
            self.sleeps.append, calendars=CALS, **kw,  # type: ignore[arg-type]
        )  # fmt: skip

    def rows(self) -> list[tuple[object, ...]]:
        c = sqlite3.connect(self.data / "nivesh.sqlite")
        try:
            return c.execute(
                "SELECT job, trading_date, status, attempts, reason, late, notified, finished_at "
                "FROM job_run ORDER BY job, trading_date"
            ).fetchall()
        finally:
            c.close()


@pytest.fixture
def t(cli_env: Env) -> Tick:
    return Tick(cli_env)


def ok(_: JobCtx) -> JobResult:
    return JobResult("ok", "done")


def flaky(failures: int, exc: Exception, t: Tick):  # type: ignore[no-untyped-def]
    def job(_: JobCtx) -> JobResult:
        t.calls += 1
        if t.calls <= failures:
            raise exc
        return JobResult("ok", "recovered")

    return job


def test_success_first_try_writes_one_ok_row_and_no_notice(t: Tick) -> None:
    reports = t.run({"t1": ok}, sched(spec()))
    assert [(r.job, r.status, r.attempts) for r in reports] == [("t1", "ok", 1)]
    assert t.sent == [] and t.sleeps == []
    (row,) = t.rows()
    assert row[:7] == ("t1", "2026-10-09", "ok", 1, "done", 0, 0)
    assert row[7] == EIGHT.isoformat()  # stored in UTC


def test_two_failures_then_success_sleeps_the_configured_waits(t: Tick) -> None:
    reports = t.run({"t1": flaky(2, RuntimeError("x"), t)}, sched(spec()))
    assert reports[0].status == "ok" and reports[0].attempts == 3
    assert t.sleeps == [30, 120] and t.sent == []


def test_three_failures_send_exactly_one_notice_and_the_second_tick_is_a_no_op(t: Tick) -> None:
    reg: Registry = {"t1": flaky(99, RuntimeError("x"), t)}
    rep = t.run(reg, sched(spec()))[0]
    assert (rep.status, rep.attempts, rep.notified) == ("failed", 3, True)
    assert t.sleeps == [30, 120] and t.calls == 3
    (msg,) = t.sent
    assert "t1" in msg.subject and "RuntimeError" in msg.text and "2026-10-09" in msg.text
    assert "13:30 IST" not in msg.text and "08:00 IST" in msg.text  # displayed in IST
    assert t.run(reg, sched(spec())) == [] and t.calls == 3 and len(t.sent) == 1
    assert t.rows()[0][2:4] == ("failed", 3)


def test_budget_refusal_is_final_after_one_attempt(t: Tick) -> None:
    def job(_: JobCtx) -> JobResult:
        t.calls += 1
        raise BudgetRefused("monthly cost 1000/1000 INR: refused")

    rep = t.run({"t1": job}, sched(spec()))[0]
    assert (rep.status, rep.attempts, t.calls, t.sleeps) == ("failed", 1, 1, [])
    assert "budget cap" in t.sent[0].text and len(t.sent) == 1


def test_calendar_unknown_inside_a_job_is_final_too(t: Tick) -> None:
    rep = t.run({"t1": flaky(9, CalendarUnknown("no NSE data"), t)}, sched(spec()))[0]
    assert (rep.attempts, t.calls) == (1, 1) and "CalendarUnknown" in rep.reason


def test_database_contention_is_retried_and_reported_plainly(t: Tick) -> None:
    busy = NiveshError("the market database is in use by another process; retry shortly")
    rep = t.run({"t1": flaky(9, busy, t)}, sched(spec()))[0]
    assert (rep.status, rep.attempts, rep.reason) == ("failed", 3, "market database in use")
    assert "market database in use" in t.sent[0].text
    assert t.run({"t1": ok}, sched(spec())) == []  # final for the date: no later-tick retry


def test_a_plain_error_is_retried_too(t: Tick) -> None:
    rep = t.run({"t1": flaky(1, NiveshError("odd"), t)}, sched(spec()))[0]
    assert rep.status == "ok" and rep.attempts == 2 and t.sleeps == [30]


@pytest.mark.parametrize(
    ("outcomes", "notified", "why"),
    [
        ([Outcome("slack", "sent", 1)], 1, ""),
        ([], 0, "no channel configured"),
        ([Outcome("slack", "failed", 3, "http 500")], 0, "notice not delivered"),
        ([Outcome("slack", "blocked", 0, "text:pan@1")], 0, "notice not delivered"),
    ],
)
def test_notified_means_a_channel_took_it(
    t: Tick, outcomes: list[Outcome], notified: int, why: str
) -> None:
    t.outcomes = outcomes
    t.run({"t1": flaky(99, RuntimeError("x"), t)}, sched(spec()))
    row = t.rows()[0]
    assert row[6] == notified and (why in str(row[4]))


def test_a_notifier_that_raises_leaves_a_failed_row(t: Tick) -> None:
    def boom(_: Message) -> list[Outcome]:
        raise RuntimeError("down")

    rep = run_due(
        t.settings, t.profile, sched(spec()), {"t1": flaky(99, RuntimeError("x"), t)}, EIGHT,
        boom, t.sleeps.append, calendars=CALS,
    )[0]  # fmt: skip
    assert rep.status == "failed" and not rep.notified
    assert t.rows()[0][2] == "failed" and "notice failed" in str(t.rows()[0][4])


def test_stale_window_is_per_job(t: Tick) -> None:
    init_stores(t.data)
    c = sqlite3.connect(t.data / "nivesh.sqlite", isolation_level=None)
    long_ago = EIGHT - timedelta(minutes=61)
    claim_job(c, "slow", DAY, long_ago, 240)
    claim_job(c, "quick", DAY, long_ago, 60)
    c.close()
    seen: list[str] = []

    def mark(ctx: JobCtx) -> JobResult:
        seen.append(ctx.spec.name)
        return JobResult("ok")

    jobs = sched(spec("slow", stale_minutes=240), spec("quick"))
    t.run({"slow": mark, "quick": mark}, jobs)
    assert seen == ["quick"]  # the slow job may still be running; the quick one is reclaimed
    assert {r[0]: r[2] for r in t.rows()} == {"slow": "running", "quick": "ok"}
    assert [r[3] for r in t.rows() if r[0] == "quick"] == [2]  # the crashed run counts


def test_the_job_clock_drives_run_rows_and_dirs(t: Tick) -> None:
    def job(ctx: JobCtx) -> JobResult:
        with plain_run(ctx.settings, "probe", clock=ctx.clock) as m:
            m.status = "ok"
        return JobResult("ok", run_id=m.run_id)

    t.run({"t1": job}, sched(spec()))
    c = sqlite3.connect(t.data / "nivesh.sqlite")
    assert c.execute("SELECT started_at, run_dir FROM run").fetchone() == (
        EIGHT.isoformat(), "runs/2026-10-09/1",
    )  # fmt: skip
    assert c.execute("SELECT run_id FROM job_run").fetchone() == (1,)


def test_the_claim_is_committed_and_leaves_the_store_writable(t: Tick) -> None:
    def job(ctx: JobCtx) -> JobResult:
        other = sqlite3.connect(Path(ctx.settings.data_dir) / "nivesh.sqlite", timeout=0.2)
        try:
            other.execute("INSERT INTO run (command, started_at, status) VALUES ('w', 't', 'ok')")
            other.commit()
            seen = other.execute("SELECT status FROM job_run").fetchone()
        finally:
            other.close()
        assert seen == ("running",)  # the claim is visible to others while the job runs
        return JobResult("ok")

    assert t.run({"t1": job}, sched(spec()))[0].status == "ok"


def test_a_second_tick_during_the_job_leaves_it_alone(t: Tick) -> None:
    inner: list[list[JobReport]] = []

    def job(_: JobCtx) -> JobResult:
        t.calls += 1
        inner.append(t.run({"t1": job}, sched(spec())))  # an overlapping tick
        return JobResult("ok")

    t.run({"t1": job}, sched(spec()))
    assert t.calls == 1 and inner == [[]]


def test_next_trading_date_runs_after_a_finished_one(t: Tick) -> None:
    t.run({"t1": ok}, sched(spec()))
    assert t.run({"t1": ok}, sched(spec())) == []
    monday = EIGHT + timedelta(days=3)
    assert [r.trading_date for r in t.run({"t1": ok}, sched(spec()), monday)] == [
        date(2026, 10, 12)
    ]


def test_unknown_calendar_year_is_skipped_with_one_notice_only(t: Tick) -> None:
    jan = datetime(2027, 1, 4, 2, 30, tzinfo=UTC)
    jobs = sched(spec("a"), spec("b"))
    reports = t.run({"a": ok, "b": ok}, jobs, jan)
    assert [r.status for r in reports] == ["skipped", "skipped"]
    assert len(t.sent) == 1 and "2027" in t.sent[0].text and "incomplete" in t.sent[0].text
    assert t.run({"a": ok, "b": ok}, jobs, jan) == []
    t.run({"a": ok, "b": ok}, jobs, jan + timedelta(days=1))
    assert len(t.sent) == 1  # one notice per gap, not per job or day


def test_a_missed_window_is_recorded_without_a_notice(t: Tick) -> None:
    night = datetime(2026, 10, 9, 16, 30, tzinfo=UTC)  # 22:00 IST
    jobs = sched(spec(catchup_hours=3))
    reports = t.run({"t1": ok}, jobs, night)
    assert [(r.status, r.reason, r.late) for r in reports] == [("skipped", "missed window", True)]
    assert t.sent == [] and t.rows()[0][2] == "skipped"


def test_dry_run_writes_and_sends_nothing(t: Tick) -> None:
    reports = t.run({"t1": ok}, sched(spec()), dry_run=True)
    assert [(r.job, r.status) for r in reports] == [("t1", "due")]
    assert not (t.data / "nivesh.sqlite").exists() and t.sent == []
    t.run({"t1": ok}, sched(spec()))
    again = t.run({"t1": ok}, sched(spec()), dry_run=True)
    assert again == [] and len(t.rows()) == 1


def test_dry_run_lists_skips(t: Tick) -> None:
    jan = datetime(2027, 1, 4, 2, 30, tzinfo=UTC)
    (rep,) = t.run({"t1": ok}, sched(spec()), jan, dry_run=True)
    assert rep.status == "skip" and "incomplete" in rep.reason


def test_disabled_and_unregistered_jobs(t: Tick) -> None:
    assert t.run({"t1": ok}, sched(spec(enabled=False))) == []
    rep = t.run({}, sched(spec()))[0]
    assert rep.status == "failed" and "no such job" in rep.reason and len(t.sent) == 1


def test_a_skip_that_may_be_redone_waits_inside_the_grace_window(t: Tick) -> None:
    calls: list[int] = []

    def stale(_: JobCtx) -> JobResult:
        calls.append(1)
        return JobResult("skipped", "bars stale", final=False)

    jobs = sched(spec(grace_minutes=30))
    assert t.run({"t1": stale}, jobs)[0].status == "waiting" and t.rows() == []
    assert t.run({"t1": stale}, jobs, EIGHT + timedelta(minutes=10))[0].status == "waiting"
    last = t.run({"t1": stale}, jobs, EIGHT + timedelta(minutes=45))[0]
    assert (last.status, last.reason) == ("skipped", "bars stale") and len(calls) == 3
    assert t.rows()[0][2] == "skipped" and t.sent == []
    assert t.run({"t1": stale}, jobs, EIGHT + timedelta(minutes=50)) == []


def test_failure_text_is_curated_redacted_and_clean() -> None:
    e = NiveshError("cannot read /Users/someone/data/file.csv for folio 123456789012")
    text = describe(e)
    assert "<path>" in text and "123456789012" not in text and "someone" not in text
    assert describe(ValueError("odd text 123456789012")) == "ValueError"
    msg = failure_message("brief_in", DAY, text, EIGHT)
    assert scan_text(msg.subject) == [] and scan_text(msg.text) == []
    assert "08:00 IST" in msg.text and msg.html.startswith("<pre>")
