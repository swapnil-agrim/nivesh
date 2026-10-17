"""`nivesh quota status|set` (ADR-0016): read the vendor call ledger and keep it honest. SQLite
only (never a DuckDB connection of its own); fixtures and a frozen clock, no network."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.quota as nq
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.fundamentals_config import PlanSettings
from nivesh_core.quota import Budget, add_manual
from tests.scan_fx import scan_output

runner = CliRunner()
Env = tuple[list[str], Path]
NOW = datetime(2026, 10, 9, 6, 30, tzinfo=UTC)  # 12:00 IST on 2026-10-09
PLAN = PlanSettings(daily=400, monthly=5000)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nq, "utcnow", lambda: NOW)


def run(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], "quota", *args])


def ledger(env: Env) -> sqlite3.Connection:
    init_stores(env[1])
    return open_sqlite(env[1] / "nivesh.sqlite")


def spend(env: Env, n: int, manual: int = 0) -> None:
    c = ledger(env)
    b = Budget(c, "tapetide", PLAN, now=lambda: NOW)
    for _ in range(n):
        b.reserve("get_financials", "RELIANCE")
    if manual:
        add_manual(c, "tapetide", manual, now=lambda: NOW)
    c.close()


def rows(env: Env, table: str, where: str = "1") -> list[tuple[Any, ...]]:
    c = ledger(env)
    try:
        return c.execute(f"SELECT * FROM {table} WHERE {where}").fetchall()  # noqa: S608
    finally:
        c.close()


def test_status_on_a_fresh_store_shows_both_windows_and_their_resets(
    cli_env: Env, tmp_path: Path
) -> None:
    r = run(cli_env, "status")
    assert r.exit_code == 0, r.output
    assert "tapetide" in r.output
    assert "today 2026-10-09: 0 of 400 used, 400 left" in r.output
    assert "month 2026-10-01 to 2026-10-31: 0 of 5,000 used, 5,000 left" in r.output
    assert "limit from config; resets 00:00 IST" in r.output
    assert "limit from config; resets 00:00 IST on 2026-11-01" in r.output
    assert "warning" not in r.output and scan_output(r.output, tmp_path) == []


def test_status_after_calls_shows_used_remaining_manual_and_the_80_percent_warning(
    cli_env: Env, tmp_path: Path
) -> None:
    spend(cli_env, 3, manual=2)
    r = run(cli_env, "status")
    assert "today 2026-10-09: 5 of 400 used, 395 left" in r.output
    assert "month 2026-10-01 to 2026-10-31: 5 of 5,000 used, 4,995 left" in r.output
    assert r.output.count("manual calls 2") == 2  # both windows say so
    assert run(cli_env, "set", "tapetide", "--daily", "6").exit_code == 0
    again = run(cli_env, "status")
    assert "5 of 6 used, 1 left; limit from override" in again.output
    assert "warning: tapetide daily quota at 5 of 6 (83%)" in again.output
    assert scan_output(again.output, tmp_path) == []


def test_status_vendor_filter_and_an_unknown_vendor_is_an_error(cli_env: Env) -> None:
    ok = run(cli_env, "status", "--vendor", "tapetide")
    assert ok.exit_code == 0 and "tapetide" in ok.output
    bad = run(cli_env, "status", "--vendor", "x")
    assert bad.exit_code == 1
    assert "error: unknown vendor 'x'; vendors with a plan: tapetide" in bad.output


def test_status_json_has_exact_integer_fields(cli_env: Env, tmp_path: Path) -> None:
    spend(cli_env, 3, manual=2)
    r = run(cli_env, "status", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    (v,) = doc["vendors"]
    assert v["vendor"] == "tapetide" and v["warnings"] == []
    assert v["day"] == {
        "starts_on": "2026-10-09", "resets_on": "2026-10-10", "used": 5, "manual": 2,
        "limit": 400, "remaining": 395, "limit_source": "config",
    }  # fmt: skip
    assert v["month"]["used"] == 5 and v["month"]["limit"] == 5000
    assert v["month"]["starts_on"] == "2026-10-01" and v["month"]["resets_on"] == "2026-11-01"
    assert all(
        type(v[w][k]) is int for w in ("day", "month") for k in ("used", "limit", "remaining")
    )
    assert scan_output(r.output, tmp_path) == []


def test_set_daily_appends_an_override_and_leaves_the_config_alone(
    cli_env: Env, tmp_path: Path
) -> None:
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    before = cfg.read_text()
    r = run(cli_env, "set", "tapetide", "--daily", "300")
    assert r.exit_code == 0, r.output
    assert (
        "wrote tapetide limit override: daily 300" in r.output
        and scan_output(r.output, tmp_path) == []
    )
    assert (
        "today 2026-10-09: 0 of 300 used, 300 left; limit from override"
        in run(cli_env, "status").output
    )
    assert (
        "month 2026-10-01 to 2026-10-31: 0 of 5,000 used, 5,000 left; limit from config"
        in run(cli_env, "status").output
    )
    assert cfg.read_text() == before and len(rows(cli_env, "vendor_limit")) == 1


def test_set_monthly_and_both_limits_are_one_row(cli_env: Env) -> None:
    assert run(cli_env, "set", "tapetide", "--monthly", "2000").exit_code == 0
    both = run(cli_env, "set", "tapetide", "--daily", "250", "--monthly", "3000")
    assert "daily 250, monthly 3,000" in both.output
    assert len(rows(cli_env, "vendor_limit")) == 2
    st = json.loads(run(cli_env, "status", "--json").output)["vendors"][0]
    assert st["day"]["limit"] == 250 and st["month"]["limit"] == 3000  # the latest wins


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_a_limit_below_one_is_rejected_and_nothing_is_written(cli_env: Env, bad: str) -> None:
    r = run(cli_env, "set", "tapetide", "--daily", bad)
    assert r.exit_code == 1 and "a limit must be 1 or more" in r.output
    assert rows(cli_env, "vendor_limit") == []


@pytest.mark.parametrize("option", ["--daily", "--monthly", "--used-today"])
@pytest.mark.parametrize("big", ["1000001", "9223372036854775807", "9" * 30])
def test_an_oversized_number_is_a_clean_error_and_nothing_is_written(
    cli_env: Env, option: str, big: str
) -> None:
    r = run(cli_env, "set", "tapetide", option, big)
    assert r.exit_code == 1 and "at most 1,000,000" in r.output, r.output
    assert "Traceback" not in r.output and not isinstance(r.exception, OverflowError)
    assert rows(cli_env, "vendor_call") == [] and rows(cli_env, "vendor_limit") == []


def test_set_needs_at_least_one_option_and_a_known_vendor(cli_env: Env) -> None:
    none = run(cli_env, "set", "tapetide")
    assert none.exit_code == 1 and "give --daily, --monthly or --used-today" in none.output
    bad = run(cli_env, "set", "x", "--daily", "5")
    assert bad.exit_code == 1 and "unknown vendor 'x'; vendors with a plan: tapetide" in bad.output
    assert rows(cli_env, "vendor_limit") == []


def test_used_today_tops_the_day_up_with_one_manual_row(cli_env: Env, tmp_path: Path) -> None:
    r = run(cli_env, "set", "tapetide", "--used-today", "120")
    assert r.exit_code == 0, r.output
    assert "wrote 120 manual calls for tapetide on 2026-10-09 (the ledger showed 0)" in r.output
    manual = rows(cli_env, "vendor_call", "kind = 'manual'")
    assert len(manual) == 1 and scan_output(r.output, tmp_path) == []
    assert "today 2026-10-09: 120 of 400 used, 280 left" in run(cli_env, "status").output


def test_used_today_adds_only_the_difference_and_never_lowers(cli_env: Env) -> None:
    spend(cli_env, 100)
    r = run(cli_env, "set", "tapetide", "--used-today", "120")
    assert "wrote 20 manual calls for tapetide on 2026-10-09 (the ledger showed 100)" in r.output
    again = run(cli_env, "set", "tapetide", "--used-today", "120")
    assert again.exit_code == 0 and "nothing written" in again.output
    assert "already shows 120 calls for tapetide on 2026-10-09" in again.output
    lower = run(cli_env, "set", "tapetide", "--used-today", "50")
    assert "nothing written" in lower.output and "already shows 120" in lower.output
    assert len(rows(cli_env, "vendor_call", "kind = 'manual'")) == 1
    assert "120 of 400 used" in run(cli_env, "status").output


def test_on_back_dates_the_manual_row_to_that_ist_day(cli_env: Env) -> None:
    r = run(cli_env, "set", "tapetide", "--used-today", "30", "--on", "2026-10-08")
    assert r.exit_code == 0, r.output
    assert "wrote 30 manual calls for tapetide on 2026-10-08" in r.output
    (row,) = rows(cli_env, "vendor_call", "kind = 'manual'")
    assert "2026-10-08" in row and 30 in row
    st = json.loads(run(cli_env, "status", "--json").output)["vendors"][0]
    assert st["day"]["used"] == 0 and st["month"]["used"] == 30  # last night counts to the month
    spend(cli_env, 4)  # today's own calls do not hide that day's top-up
    same = run(cli_env, "set", "tapetide", "--used-today", "30", "--on", "2026-10-08")
    assert "nothing written" in same.output


@pytest.mark.parametrize(
    ("args", "why"),
    [
        (["--used-today", "10", "--on", "2026-10-10"], "--on cannot be in the future"),
        (["--used-today", "10", "--on", "yesterday"], "--on must be an IST date"),
        (["--on", "2026-10-08", "--daily", "5"], "--on needs --used-today"),
        (["--used-today", "0"], "--used-today must be 1 or more"),
    ],
)
def test_bad_manual_options_are_rejected_before_anything_is_written(
    cli_env: Env, args: list[str], why: str
) -> None:
    r = run(cli_env, "set", "tapetide", *args)
    assert r.exit_code == 1 and why in r.output
    assert rows(cli_env, "vendor_call") == [] and rows(cli_env, "vendor_limit") == []


def test_used_today_and_a_limit_can_be_given_together(cli_env: Env) -> None:
    r = run(cli_env, "set", "tapetide", "--daily", "200", "--used-today", "50")
    assert r.exit_code == 0, r.output
    assert "daily 200" in r.output and "wrote 50 manual calls" in r.output
    assert "50 of 200 used" in run(cli_env, "status").output


def test_status_and_set_work_while_the_market_store_is_write_locked(cli_env: Env) -> None:
    init_stores(cli_env[1])
    lock = open_duck(cli_env[1] / "nivesh.duckdb")  # another process holds the single writer
    try:
        assert run(cli_env, "status").exit_code == 0
        assert run(cli_env, "set", "tapetide", "--daily", "10").exit_code == 0
        assert run(cli_env, "status", "--json").exit_code == 0
    finally:
        lock.close()


def test_status_and_set_never_open_the_market_store_for_writing(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_stores(cli_env[1])

    def guard(path: Path, *, read_only: bool = False) -> Any:
        if not read_only:
            pytest.fail("opened DuckDB read-write")
        return open_duck(path, read_only=True)

    monkeypatch.setattr("nivesh_core.db.open_duck", guard)
    assert run(cli_env, "status").exit_code == 0
    assert run(cli_env, "set", "tapetide", "--monthly", "9").exit_code == 0


def test_quota_group_lists_its_commands_and_status_command_is_unchanged(cli_env: Env) -> None:
    r = run(cli_env, "--help")
    assert r.exit_code == 0 and "status" in r.output and "set" in r.output
    root = runner.invoke(app, [*cli_env[0], "--help"])
    assert "quota" in root.output and "watch" in root.output  # the other groups are untouched


def test_a_config_without_any_plan_says_so(cli_env: Env) -> None:
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    text = cfg.read_text().replace(
        "      plan: {daily: 400, monthly: 5000, min_interval_s: 1.1, month_start_day: 1, "
        "reserve_pct: {watchlist: 10, universe: 30}}\n",
        "",
    )
    assert text != cfg.read_text()
    cfg.write_text(text)
    r = run(cli_env, "status")
    assert r.exit_code == 0 and "no vendor has a plan" in r.output
    bad = run(cli_env, "set", "tapetide", "--daily", "5")
    assert bad.exit_code == 1 and "vendors with a plan: none" in bad.output
