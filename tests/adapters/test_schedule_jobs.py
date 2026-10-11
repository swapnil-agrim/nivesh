"""The scheduled jobs through the real runner: faked clock, injected notifier, no network."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import nivesh_adapters.alerts_service as alerts_svc
from nivesh_adapters.delivery import Outcome
from nivesh_adapters.investright_session import TokenStore, token_path
from nivesh_adapters.schedule_jobs import default_registry
from nivesh_adapters.scheduler import JobReport
from nivesh_core import backup
from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_text
from nivesh_core.schedule_config import ScheduleSettings
from nivesh_core.thesis_store import save_thesis
from nivesh_core.timeutil import IST
from tests.adapters.test_holding_reviews import Held, held  # noqa: F401
from tests.adapters.test_scheduler import EIGHT, Env, Tick
from tests.alerts_fx import hold_symbols, make_alert_fx, put_closes, quiet_then
from tests.core.test_thesis import crit, thesis
from tests.ideas_fx import hold, seed_ideas_store, with_benchmarks
from tests.scoring_fx import ISSUE, add_entry, line_bars, make_fx

ALL = {j.name: j.model_copy(update={"enabled": True}) for j in ScheduleSettings().jobs}


def only(*names: str) -> ScheduleSettings:
    return ScheduleSettings(jobs=[ALL[n] for n in names])


@pytest.fixture
def t(cli_env: Env) -> Tick:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    return Tick(cli_env)


def run(t: Tick, jobs: ScheduleSettings, at: datetime) -> list[JobReport]:
    return t.run(default_registry(t.notifier), jobs, at)


def ist(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(UTC)


def count(t: Tick, sql: str) -> object:
    c = sqlite3.connect(t.data / "nivesh.sqlite")
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


def test_brief_in_saves_a_report_and_delivers_it(t: Tick) -> None:
    (rep,) = run(t, only("brief_in"), EIGHT)
    assert (rep.status, rep.reason) == ("ok", "brief saved; delivered")
    assert len(t.sent) == 1 and t.sent[0].subject.startswith("Market brief: india")
    assert "Holdings snapshot" in t.sent[0].html and scan_text(t.sent[0].text) == []
    assert count(t, "SELECT command FROM run") == "brief"
    assert count(t, "SELECT started_at FROM run") == EIGHT.isoformat()  # the fake clock
    assert (t.data / "runs" / "2026-10-09" / "1" / "report.md").is_file()


def test_brief_without_a_session_still_succeeds_and_states_the_snapshot(t: Tick) -> None:
    hold(t.data, 0)
    assert not token_path(t.data).exists()
    (rep,) = run(t, only("brief_in"), EIGHT)
    assert rep.status == "ok" and "Holdings snapshot as of 2026-01-01" in t.sent[0].html


@pytest.mark.parametrize(
    ("outcomes", "note"),
    [
        ([], "not delivered (no channel configured)"),
        ([Outcome("slack", "blocked", 0, "text:pan@3")], "not delivered (channel refused it)"),
        ([Outcome("slack", "failed", 3, "http 500")], "not delivered (channel refused it)"),
    ],
)
def test_a_failed_delivery_is_a_note_not_a_failure(
    t: Tick, outcomes: list[Outcome], note: str
) -> None:
    t.outcomes = outcomes
    (rep,) = run(t, only("brief_in"), EIGHT)
    assert rep.status == "ok" and rep.reason == f"brief saved; {note}" and not t.sleeps
    assert count(t, "SELECT status FROM run") == "ok"


def test_a_notifier_that_raises_does_not_fail_the_brief(t: Tick) -> None:
    def boom(_: object) -> list[Outcome]:
        raise RuntimeError("down")

    jobs = only("brief_in")
    (rep,) = t.run(default_registry(boom), jobs)  # type: ignore[arg-type]
    assert rep.status == "ok" and "notice failed" in rep.reason


def test_brief_us_follows_the_dst_shift(t: Tick) -> None:
    jobs = only("brief_us")
    assert run(t, jobs, datetime(2026, 10, 30, 12, 59, tzinfo=UTC)) == []
    (rep,) = run(t, jobs, datetime(2026, 10, 30, 13, 0, tzinfo=UTC))  # EDT: 18:30 IST
    assert (rep.job, rep.trading_date, rep.status) == ("brief_us", date(2026, 10, 30), "ok")
    assert t.sent[0].subject.startswith("Market brief: us")
    assert run(t, jobs, datetime(2026, 11, 2, 13, 0, tzinfo=UTC)) == []  # EST: not yet
    (win,) = run(t, jobs, datetime(2026, 11, 2, 14, 0, tzinfo=UTC))  # 19:30 IST
    assert win.trading_date == date(2026, 11, 2) and win.status == "ok"


def test_one_nudge_across_twelve_ticks_with_the_snapshot_date(t: Tick) -> None:
    hold(t.data, 0)
    jobs = only("login_nudge")
    base = ist(2026, 10, 9, 8, 45)
    reports = [r for k in range(12) for r in run(t, jobs, base + timedelta(minutes=5 * k))]
    assert len(reports) == 1 and reports[0].status == "ok" and len(t.sent) == 1
    msg = t.sent[0]
    assert "2026-01-01" in msg.text and "nivesh login" in msg.text
    assert scan_text(msg.text) == [] and scan_text(msg.subject) == []


def test_nudge_says_when_no_snapshot_is_loaded(t: Tick) -> None:
    run(t, only("login_nudge"), ist(2026, 10, 9, 8, 45))
    assert "no holdings snapshot" in t.sent[0].text


def test_no_nudge_with_a_valid_session(t: Tick) -> None:
    at = ist(2026, 10, 9, 8, 45)
    TokenStore(token_path(t.data), clock=lambda: at - timedelta(minutes=30)).save("a" + "b")
    (rep,) = run(t, only("login_nudge"), at)
    assert rep.status == "ok" and "no reminder needed" in rep.reason and t.sent == []


def test_a_session_from_yesterday_counts_as_expired(t: Tick) -> None:
    at = ist(2026, 10, 9, 8, 45)
    TokenStore(token_path(t.data), clock=lambda: at - timedelta(days=1)).save("a" + "b")
    run(t, only("login_nudge"), at)
    assert len(t.sent) == 1


def test_no_nudge_on_an_nse_holiday_and_none_before_the_time(t: Tick) -> None:
    jobs = only("login_nudge")
    assert run(t, jobs, ist(2026, 10, 2, 8, 45)) == []
    assert run(t, jobs, ist(2026, 10, 9, 8, 44)) == []
    assert t.sent == []


def test_rescore_runs_on_a_saturday_and_a_rerun_is_a_no_op(cli_env: Env) -> None:
    fx = make_fx(cli_env[1].parent)
    line_bars(fx, "AAA", ISSUE, date(2026, 2, 20))
    line_bars(fx, "BENCHIN", ISSUE, date(2026, 2, 20))
    add_entry(fx, "AAA")
    fx.sql.close()
    fx.duck.close()
    t = Tick(cli_env)
    t.settings = fx.settings
    sat = ist(2026, 2, 21, 8, 30)  # a Saturday: not a session, yet the job runs
    (rep,) = run(t, only("rescore"), sat)
    assert rep.status == "ok" and rep.trading_date == date(2026, 2, 21)
    n = count(t, "SELECT count(*) FROM ledger_score")
    assert isinstance(n, int) and n > 0 and rep.reason.startswith("scored")
    assert run(t, only("rescore"), sat + timedelta(minutes=5)) == []
    assert count(t, "SELECT count(*) FROM ledger_score") == n


def backup_settings(t: Tick, tmp_path: Path) -> Path:
    target = tmp_path / "bk"
    cfg = t.settings.backup.model_copy(
        update={"target": str(target), "recipient": "age1" + "q" * 20}
    )
    t.settings = t.settings.model_copy(update={"backup": cfg})
    return target


def test_backup_unconfigured_is_skipped_without_a_notice(t: Tick) -> None:
    (rep,) = run(t, only("backup"), ist(2026, 10, 10, 7, 0))
    assert rep.status == "skipped" and "not configured" in rep.reason and t.sent == []


def test_backup_writes_an_archive_and_prunes_old_ones(
    t: Tick, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup, "_encrypt", lambda s, d, r: d.write_bytes(s.read_bytes()[::-1]))
    target = backup_settings(t, tmp_path)
    target.mkdir()
    old = target / "nivesh-backup-20250101T000000Z.tar.age"
    old.write_bytes(b"x")
    (rep,) = run(t, only("backup"), ist(2026, 10, 10, 7, 0))
    assert rep.status == "ok" and "pruned 1" in rep.reason
    left = [p.name for p in target.iterdir()]
    assert left == ["nivesh-backup-20261010T013000Z.tar.age"] and not old.exists()


def test_backup_error_is_retried_then_reported(
    t: Tick, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def busy(*_: object) -> Path:
        calls.append(1)
        raise NiveshError("market database in use")

    monkeypatch.setattr(backup, "create_backup", busy)
    backup_settings(t, tmp_path)
    (rep,) = run(t, only("backup"), ist(2026, 10, 10, 7, 0))
    assert (rep.status, rep.attempts, len(calls)) == ("failed", 3, 3) and t.sleeps == [30, 120]
    assert len(t.sent) == 1


def test_every_default_job_has_a_handler(t: Tick) -> None:
    reg = default_registry(t.notifier)
    missing = {j.name for j in ScheduleSettings().jobs} - set(reg)
    assert missing == set()


# ---- monitors and kill criteria ----------------------------------------------------------
D = Decimal


@pytest.fixture
def mt(cli_env: Env) -> Tick:
    fx = make_alert_fx(cli_env[1].parent)
    hold_symbols(fx, "AAA", "UUU")
    put_closes(fx, "AAA", quiet_then(D("0.08")))
    put_closes(fx, "UUU", quiet_then(D("-0.09")))
    fx.close()
    t = Tick(cli_env)
    t.settings = fx.settings
    return t


FRI_EVENING = ist(2026, 10, 9, 17, 30)


def alerts(t: Tick) -> list[tuple[object, ...]]:
    c = sqlite3.connect(t.data / "nivesh.sqlite")
    try:
        return c.execute("SELECT rule, delivered FROM alert ORDER BY alert_id").fetchall()
    finally:
        c.close()


def test_monitors_fire_send_and_store_on_a_trading_day(mt: Tick) -> None:
    (rep,) = run(mt, only("monitors"), FRI_EVENING)
    assert rep.status == "ok" and rep.reason.startswith("1 fired: 1 sent")
    assert "no numeric invalidation: AAA" in rep.reason
    assert alerts(mt) == [("big_move", 1)] and "AAA" in mt.sent[0].text
    assert run(mt, only("monitors"), FRI_EVENING + timedelta(minutes=5)) == []  # second tick


def test_monitors_do_not_run_at_the_weekend(mt: Tick) -> None:
    assert run(mt, only("monitors"), ist(2026, 10, 10, 17, 30)) == []


def test_monitors_us_cover_the_us_session_the_morning_after(mt: Tick) -> None:
    (rep,) = run(mt, only("monitors_us"), ist(2026, 10, 10, 5, 30))
    assert (rep.job, rep.trading_date, rep.status) == ("monitors_us", date(2026, 10, 9), "ok")
    assert alerts(mt) == [("big_move", 1)] and "UUU" in mt.sent[0].text


def test_old_bars_skip_the_monitors_without_a_notice_and_a_later_tick_may_retry(mt: Tick) -> None:
    from nivesh_core.db.duck import open_duck

    duck = open_duck(mt.data / "nivesh.duckdb")
    duck.execute("DELETE FROM price_bar WHERE date = DATE '2026-10-09'")
    duck.close()
    jobs = only("monitors")
    (first,) = run(mt, jobs, FRI_EVENING)
    assert first.status == "waiting" and first.reason.startswith("bars stale (latest 2026-10-08)")
    assert mt.rows() == [] and mt.sent == []
    (last,) = run(mt, jobs, FRI_EVENING + timedelta(minutes=45))  # grace is over: final
    assert (last.status, last.late) == ("skipped", True) and mt.sent == []
    assert run(mt, jobs, FRI_EVENING + timedelta(minutes=50)) == []


def test_kill_criteria_alert_on_a_met_criterion(mt: Tick, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    sid = 1
    c = sqlite3.connect(mt.data / "nivesh.sqlite", isolation_level=None)
    save_thesis(c, thesis(security_id=sid, kill_criteria=[crit(1), crit(2, machine=False)]))
    c.close()
    monkeypatch.setattr(alerts_svc, "xray_report", lambda *a, **k: {"xray": object()})
    monkeypatch.setattr(
        alerts_svc, "review_facts", lambda *a, **k: SimpleNamespace(metrics={"roce_pct": D(8)})
    )
    (rep,) = run(mt, only("kill_criteria"), ist(2026, 10, 9, 22, 0))
    assert rep.status == "ok" and rep.reason.startswith("1 fired")
    assert alerts(mt) == [("kill_criterion", 1)] and "kill criterion met" in mt.sent[0].text


def test_kill_criteria_wait_when_everything_is_stale(mt: Tick) -> None:
    from nivesh_core.db.duck import open_duck

    duck = open_duck(mt.data / "nivesh.duckdb")
    duck.execute("DELETE FROM price_bar WHERE security_id = 1")
    duck.close()
    c = sqlite3.connect(mt.data / "nivesh.sqlite", isolation_level=None)
    save_thesis(c, thesis(security_id=1, kill_criteria=[crit(1), crit(2, machine=False)]))
    c.close()
    (rep,) = run(mt, only("kill_criteria"), ist(2026, 10, 9, 22, 0))
    assert rep.status == "waiting" and rep.reason == "bars stale (latest none)"
    assert mt.rows() == [] and mt.sent == []


# ---- weekly and monthly reviews ------------------------------------------------------------------
SAT_REVIEW = ist(2024, 10, 26, 9, 5)  # a Saturday (and not a first one)
FIRST_SAT = ist(2026, 2, 7, 9, 5)


@pytest.fixture
def wt(cli_env: Env, held: Held) -> Tick:  # noqa: F811
    t = Tick(cli_env)
    t.settings, t.profile = held.settings, held.profile
    return t


def test_weekly_review_runs_the_reviewer_once_and_saves_without_delivering(
    wt: Tick,
    held: Held,  # noqa: F811
) -> None:
    (rep,) = run(wt, only("weekly_review"), SAT_REVIEW)
    assert rep.status == "ok" and rep.reason == "1 reviewed, 1 without thesis"
    assert wt.sent == [] and len(held.fake.seen) == 1  # saved, not delivered
    assert count(wt, "SELECT command FROM run") == "review holdings"
    assert count(wt, "SELECT tier FROM run") == "quick"
    assert count(wt, "SELECT count(*) FROM ledger_entry") == 1
    assert count(wt, "SELECT run_id FROM job_run") == 1
    run_dir = wt.data / "runs" / "2024-10-26" / "1"
    assert (run_dir / "trace.jsonl").is_file()
    text = (run_dir / "summary.txt").read_text()
    assert text.splitlines()[1].startswith("RELI TRIM medium") and scan_text(text) == []
    assert run(wt, only("weekly_review"), SAT_REVIEW + timedelta(minutes=5)) == []


def test_weekly_review_is_delivered_when_the_schedule_says_so(wt: Tick) -> None:
    jobs = ScheduleSettings(jobs=[ALL["weekly_review"]], deliver_holdings=True)
    (rep,) = run(wt, jobs, SAT_REVIEW)
    assert rep.reason.endswith("delivered") and len(wt.sent) == 1
    assert wt.sent[0].subject == "Weekly holdings review" and scan_text(wt.sent[0].text) == []
    assert "RELI TRIM" in wt.sent[0].text


def test_weekly_review_at_the_cap_is_final_with_one_budget_notice(
    wt: Tick,
    held: Held,  # noqa: F811
) -> None:
    c = sqlite3.connect(wt.data / "nivesh.sqlite", isolation_level=None)
    c.execute(
        "INSERT INTO run (command, started_at, status, cost_inr) VALUES ('x', ?, 'ok', 99999)",
        (SAT_REVIEW.isoformat(),),
    )
    c.close()
    wt.profile = wt.profile.model_copy(update={"monthly_cost_cap": 1000})
    (rep,) = run(wt, only("weekly_review"), SAT_REVIEW)
    assert (rep.status, rep.attempts) == ("failed", 1) and "budget cap" in rep.reason
    assert len(wt.sent) == 1 and held.fake.seen == [] and wt.sleeps == []
    assert count(wt, "SELECT count(*) FROM run") == 1  # only the seeded row


def test_a_long_review_is_not_started_twice_by_the_next_tick(wt: Tick) -> None:
    c = sqlite3.connect(wt.data / "nivesh.sqlite", isolation_level=None)
    from nivesh_core.alerts import claim_job

    claim_job(c, "weekly_review", date(2024, 10, 26), SAT_REVIEW - timedelta(minutes=90), 240)
    c.close()
    assert run(wt, only("weekly_review"), SAT_REVIEW) == []  # 90 minutes old, 240 allowed
    assert count(wt, "SELECT count(*) FROM run") == 0


def test_monthly_review_only_on_the_first_saturday(wt: Tick) -> None:
    jobs = only("monthly_fund_review")
    assert run(wt, jobs, SAT_REVIEW) == []  # the fourth Saturday
    (rep,) = run(wt, jobs, ist(2024, 10, 5, 9, 5))
    assert (rep.status, rep.reason) == ("ok", "0 funds reviewed")
    assert count(wt, "SELECT command FROM run") == "mf doctor" and wt.sent == []


def test_monthly_review_runs_the_doctor_once_for_held_funds(cli_env: Env) -> None:
    from tests.mf_fx import make_env, seed_doctor

    with make_env(cli_env[1], benchmarks={"Example Equity Index": "EXAMPLE INDEX"}) as env:
        seed_doctor(env)
        env.duck.close()  # one writer at a time: the job reads the market store
        env.sql.close()
        t = Tick(cli_env)
        t.settings = env.settings
        (rep,) = run(t, only("monthly_fund_review"), FIRST_SAT)
        assert rep.status == "ok" and rep.reason == "1 funds reviewed (SWITCH_TO_DIRECT 1)"
        run_dir = t.data / "runs" / "2026-02-07" / "1"
        assert "SWITCH_TO_DIRECT" in (run_dir / "summary.txt").read_text()
        assert run(t, only("monthly_fund_review"), FIRST_SAT + timedelta(minutes=5)) == []
