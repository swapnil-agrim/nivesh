"""`nivesh schedule`: the tick, the status page and the owner's invalidation levels."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.schedule as cs
from nivesh_adapters.delivery import Message
from nivesh_adapters.scheduler import JobCtx, JobResult, Registry
from nivesh_cli.main import app
from nivesh_core.config import load_settings
from nivesh_core.delivery_config import DeliverySettings
from tests.delivery_fx import Calls, set_refs
from tests.ideas_fx import seed_ideas_store

runner = CliRunner()
Env = tuple[list[str], Path]
FRI_EIGHT = "2026-10-09T08:00:00+05:30"
HOLIDAY = "2026-10-02T09:00:00+05:30"  # listed NSE holiday (a Friday)


@pytest.fixture
def env(cli_env: Env) -> Env:
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def fake_registry(monkeypatch: pytest.MonkeyPatch, seen: list[str]) -> None:
    def job(ctx: JobCtx) -> JobResult:
        seen.append(f"{ctx.spec.name}@{ctx.day}")
        return JobResult("ok", "fine")

    def reg(_: object) -> Registry:
        return {n: job for n in ("brief_in", "login_nudge", "monitors", "kill_criteria")}

    monkeypatch.setattr(cs, "default_registry", reg)


def test_dry_run_on_a_holiday_lists_nothing(env: Env) -> None:
    r = call(env, "schedule", "run", "--dry-run", "--now", HOLIDAY)
    assert r.exit_code == 0, r.output
    assert "brief_in" not in r.stdout and "login_nudge" not in r.stdout  # no trading day
    assert "kill_criteria 2026-10-01 due LATE" in r.stdout  # last night's run, caught up
    assert not (env[1] / "nivesh.sqlite").exists()  # a dry run writes nothing
    quiet = call(env, "schedule", "run", "--dry-run", "--now", "2026-10-04T14:00:00+05:30")
    assert quiet.stdout.strip() == "nothing due"  # a Sunday afternoon


def test_dry_run_lists_what_would_run(env: Env) -> None:
    r = call(env, "schedule", "run", "--dry-run", "--now", FRI_EIGHT)
    assert r.exit_code == 0 and "brief_in 2026-10-09 due" in r.stdout


def test_run_prints_each_job_and_a_second_run_is_a_no_op(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    fake_registry(monkeypatch, seen)
    first = call(env, "schedule", "run", "--now", FRI_EIGHT)
    assert first.exit_code == 0, first.output
    assert "brief_in 2026-10-09 ok (fine)" in first.stdout and "brief_in@2026-10-09" in seen
    n = len(seen)
    again = call(env, "schedule", "run", "--now", FRI_EIGHT)
    assert again.stdout.strip() == "already done" and len(seen) == n


def test_now_must_be_an_aware_timestamp(env: Env) -> None:
    for bad in ("2026-10-09T08:00:00", "soon"):
        r = call(env, "schedule", "run", "--now", bad)
        assert r.exit_code == 2, bad


def test_status_before_any_run_shows_ist_and_the_holiday_warning(env: Env) -> None:
    r = call(env, "schedule", "status", "--now", FRI_EIGHT)
    assert r.exit_code == 0, r.output
    assert "schedule status at 2026-10-09 08:00 IST" in r.stdout
    assert "brief_in: never run; next 2026-10-12 08:00 IST" in r.stdout
    assert "brief_us: disabled" in r.stdout
    assert "NSE holiday list has only 5 dates for 2026" in r.stdout


def test_status_shows_late_and_skip_reasons_and_json_is_stable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_registry(monkeypatch, [])
    call(env, "schedule", "run", "--now", "2026-10-09T08:40:00+05:30")  # after the grace window
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    c.execute(
        "INSERT INTO job_run (job, trading_date, status, started_at, reason) "
        "VALUES ('monitors', '2026-10-08', 'skipped', '2026-10-08T12:00:00+00:00', "
        "'bars stale (latest 2026-10-06)')"
    )
    c.commit()
    c.close()
    r = call(env, "schedule", "status", "--now", "2026-10-09T09:00:00+05:30")
    assert "brief_in: ok 2026-10-09 LATE (fine)" in r.stdout
    assert "monitors: skipped 2026-10-08 (bars stale (latest 2026-10-06))" in r.stdout
    a = call(env, "schedule", "status", "--json", "--now", "2026-10-09T09:00:00+05:30")
    b = call(env, "schedule", "status", "--json", "--now", "2026-10-09T09:00:00+05:30")
    assert a.stdout == b.stdout
    data = json.loads(a.stdout)
    brief = next(j for j in data["jobs"] if j["job"] == "brief_in")
    assert brief["last"]["late"] is True and brief["last"]["finished_ist"].endswith("IST")
    assert data["warnings"] and list(data) == sorted(data)


def test_status_warns_when_the_year_has_no_holiday_list(env: Env) -> None:
    r = call(env, "schedule", "status", "--now", "2027-01-04T08:00:00+05:30")
    assert "NSE holiday list has no dates for 2027" in r.stdout


def test_invalidation_stores_a_level_and_latest_wins(env: Env) -> None:
    seed_ideas_store(env[1])
    r = call(env, "schedule", "invalidation", "AAA", "90.5", "--note", "below the base")
    assert r.exit_code == 0, r.output
    assert "invalidation level for AAA set to 90.5" in r.stdout
    assert call(env, "schedule", "invalidation", "AAA", "88").exit_code == 0
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT level, note FROM invalidation_level ORDER BY id").fetchall() == [
        ("90.5", "below the base"), ("88", None),
    ]  # fmt: skip


@pytest.mark.parametrize("level", ["0", "-3", "abc", "NaN"])
def test_invalidation_rejects_bad_levels(env: Env, level: str) -> None:
    seed_ideas_store(env[1])
    r = call(env, "schedule", "invalidation", "AAA", level)
    assert r.exit_code != 0
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT count(*) FROM invalidation_level").fetchone() == (0,)


def test_invalidation_rejects_personal_data_and_unknown_symbols(env: Env) -> None:
    seed_ideas_store(env[1])
    pan = "ABCDE" + "1234" + "F"
    r = call(env, "schedule", "invalidation", "AAA", "90", "--note", f"owner {pan}")
    assert r.exit_code == 1 and "personal data" in r.output
    assert call(env, "schedule", "invalidation", "NOSUCH", "90").exit_code == 1


def test_the_schedule_group_is_registered_once() -> None:
    groups = [g.name for g in app.registered_groups]
    assert groups.count("schedule") == 1
    top = [c.name or c.callback.__name__ for c in app.registered_commands]  # type: ignore[union-attr]
    assert len(top) == len(set(top))


def test_real_notifier_sends_nothing_without_a_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(Path(env[0][1]) / "nivesh.yaml")

    def no_client(_: int) -> None:
        raise AssertionError("no client may be built without a channel")

    monkeypatch.setattr(cs, "http_client", no_client)
    assert cs.real_notifier(settings)(Message("s", "t", "<p>t</p>")) == []


def test_real_notifier_uses_the_configured_channels(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_refs(monkeypatch)
    cfg = DeliverySettings(channels=["slack"], slack_hook="ref:S_HOOK")
    settings = load_settings(Path(env[0][1]) / "nivesh.yaml").model_copy(update={"delivery": cfg})
    calls = Calls()
    monkeypatch.setattr(cs, "http_client", lambda _t: calls.client())
    out = cs.real_notifier(settings)(Message("job failed", "it did not run", "<p>x</p>"))
    assert [(o.channel, o.status) for o in out] == [("slack", "sent")]
    assert len(calls.requests) == 1


def test_headless_tick_runs_the_brief_end_to_end_without_network_or_model(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nivesh_agents import runtime
    from tests.ideas_fx import with_benchmarks
    from tests.run_paths import run_path

    seed_ideas_store(env[1])
    with_benchmarks(Path(env[0][1]))
    (Path(env[0][1]) / "schedule.yaml").write_text(
        "jobs:\n  - {name: brief_in, market: IN, at: '08:00', catchup_hours: 3}\n"
    )

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("a model call was made")

    monkeypatch.setattr(runtime, "query", boom)
    first = call(env, "schedule", "run", "--now", FRI_EIGHT)  # sockets are blocked by conftest
    assert first.exit_code == 0, first.output
    assert "brief_in 2026-10-09 ok (brief saved; not delivered (no channel configured))" in (
        first.stdout
    )  # fmt: skip
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT command, status, tier, started_at FROM run").fetchall() == [
        ("brief", "ok", "none", "2026-10-09T02:30:00+00:00")
    ]
    assert c.execute("SELECT job, status, notified FROM job_run").fetchall() == [
        ("brief_in", "ok", 0)
    ]
    assert (run_path(env[1], 1) / "report.md").is_file()
    assert run_path(env[1], 1).parent.name == "2026-10-09"  # dated from --now, not the real clock
    again = call(env, "schedule", "run", "--now", FRI_EIGHT)
    assert again.stdout.strip() == "already done"
    assert c.execute("SELECT count(*) FROM run").fetchone() == (1,)


# ---- nivesh alerts ----------------------------------------------------------------------
def store_alerts(env: Env) -> None:
    from datetime import UTC, date, datetime

    from nivesh_core.alerts import Alert, record_alert
    from nivesh_core.db.sqlite import open_sqlite

    seed_ideas_store(env[1])
    sql = open_sqlite(env[1] / "nivesh.sqlite")
    sid = sql.execute("SELECT id FROM security WHERE symbol = 'AAA'").fetchone()[0]
    base = {"security_id": sid, "trading_date": date(2026, 10, 9)}
    at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # 17:30 IST
    payload = {"what": "AAA closed +8.00% on the day", "verdict": "HOLD", "next_step": "x"}
    rows = [
        {"rule": "big_move", "fired_at": at, "payload": payload, "delivered": 1},
        {"rule": "break_52w", "fired_at": at, "payload": payload, "delivered": 2, "digest_id": 3},
        {
            "rule": "big_move",
            "fired_at": at,
            "payload": {**payload, "suppressed": "dedupe"},
            "delivered": 0,
        },  # fmt: skip
    ]
    for r in rows:
        record_alert(sql, Alert.model_validate({**base, **r}))
    sql.close()


def test_alerts_with_nothing_stored(env: Env) -> None:
    r = call(env, "alerts")
    assert r.exit_code == 0 and r.stdout.strip() == "no alerts"
    assert call(env, "alerts", "--json").stdout.strip() == "[]"


def test_alerts_show_ist_times_symbol_verdict_and_status(env: Env) -> None:
    store_alerts(env)
    r = call(env, "alerts", "--since", "2026-10-09")
    assert r.exit_code == 0, r.output
    assert "2026-10-09 17:30 IST  big_move  AAA  sent" in r.stdout
    assert "break_52w  AAA  sent in digest 3" in r.stdout
    assert "held back (dedupe)" in r.stdout
    assert "AAA closed +8.00% on the day | verdict: HOLD" in r.stdout


def test_alerts_filters_and_json_are_stable_and_carry_both_times(env: Env) -> None:
    store_alerts(env)
    a = call(env, "alerts", "--since", "2026-10-09", "--rule", "big_move", "--json")
    b = call(env, "alerts", "--since", "2026-10-09", "--rule", "big_move", "--json")
    assert a.stdout == b.stdout
    rows = json.loads(a.stdout)
    assert [x["rule"] for x in rows] == ["big_move", "big_move"]
    assert rows[0]["fired_at_utc"] == "2026-10-09T12:00:00+00:00"
    assert rows[0]["fired_at_ist"] == "2026-10-09 17:30 IST" and rows[0]["symbol"] == "AAA"
    assert all(list(x) == sorted(x) for x in rows)
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT fired_at FROM alert LIMIT 1").fetchone() == (
        "2026-10-09T12:00:00+00:00",
    )  # stored in UTC
    assert call(env, "alerts", "--since", "2026-10-10").stdout.strip() == "no alerts"
    assert call(env, "alerts", "--since", "soon").exit_code == 2


def test_alerts_command_is_registered_once() -> None:
    names = [c.name or c.callback.__name__ for c in app.registered_commands]  # type: ignore[union-attr]
    assert names.count("alerts") == 1


def test_a_saturday_tick_runs_one_review_and_the_second_tick_is_a_no_op(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the same stores and fake reviewer as the review command tests
    from nivesh_agents import runtime
    from nivesh_core.thesis_store import save_thesis
    from tests.agents.fake_sdk import script
    from tests.cli.test_engine_cli import ASOF as REVIEW_DAY
    from tests.cli.test_engine_cli import seed_cli_store
    from tests.cli.test_review_cli import reviewer
    from tests.core.test_thesis import thesis

    seed_cli_store(env[1])
    sql = sqlite3.connect(env[1] / "nivesh.sqlite", isolation_level=None)
    sid = sql.execute("SELECT id FROM security WHERE symbol = 'RELI'").fetchone()[0]
    from datetime import timedelta

    created = REVIEW_DAY - timedelta(days=10)
    save_thesis(sql, thesis(security_id=sid, created_at=created,
                            review_date=created + timedelta(days=90)))  # fmt: skip
    sql.close()
    fake = script(holding_review=reviewer)
    monkeypatch.setattr(runtime, "query", fake)
    (Path(env[0][1]) / "schedule.yaml").write_text(
        "jobs:\n  - {name: weekly_review, at: '09:00', weekday: 5, trading_days_only: false,"
        " catchup_hours: 15, stale_minutes: 240}\n"
    )
    saturday = "2024-10-26T09:05:00+05:30"
    first = call(env, "schedule", "run", "--now", saturday)
    assert first.exit_code == 0, first.output
    assert "weekly_review 2024-10-26 ok (1 reviewed, 1 without thesis)" in first.stdout
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT command, tier, started_at FROM run").fetchall() == [
        ("review holdings", "quick", "2024-10-26T03:35:00+00:00")
    ]  # fmt: skip
    assert call(env, "schedule", "run", "--now", saturday).stdout.strip() == "already done"
    assert len(fake.seen) == 1
