import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

import nivesh_adapters.run_support as rs
import nivesh_cli.common as common
from nivesh_adapters.holdings_service import held_securities
from nivesh_adapters.run_support import BudgetRefused, metered_run, plain_run
from nivesh_adapters.stores import open_read_stores
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.errors import NiveshError
from nivesh_core.profile import Profile, load_profile
from nivesh_core.timeutil import to_iso

ROOT = Path(__file__).resolve().parents[2]
Env = tuple[list[str], Path]
FAKE = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)  # 09:30 IST on the 2nd


def loaded(env: Env, cap: int | None = None) -> tuple[Settings, Profile]:
    cfg = Path(env[0][1])
    if cap is not None:
        p = cfg / "profile.yaml"
        p.write_text(p.read_text().replace("monthly_cost_cap: 2000", f"monthly_cost_cap: {cap}"))
    return load_settings(cfg / "nivesh.yaml"), load_profile(cfg / "profile.yaml")


def spend(data: Path, inr: int, at: datetime = FAKE) -> None:
    init_stores(data)
    c = sqlite3.connect(data / "nivesh.sqlite")
    c.execute(
        "INSERT INTO run (command, started_at, status, cost_inr) VALUES ('x', ?, 'ok', ?)",
        (to_iso(at), inr),
    )
    c.commit()
    c.close()


def run_rows(data: Path) -> list[tuple[str, str, str]]:
    c = sqlite3.connect(data / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, started_at, run_dir FROM run ORDER BY id").fetchall()
    finally:
        c.close()


def test_plain_run_follows_the_injected_clock(cli_env: Env) -> None:
    settings, _ = loaded(cli_env)
    with plain_run(settings, "probe", clock=lambda: FAKE) as m:
        m.status = "ok"
    assert m.run_dir == cli_env[1] / "runs" / "2026-01-02" / "1" and m.run_dir.is_dir()
    assert run_rows(cli_env[1]) == [("probe", to_iso(FAKE), "runs/2026-01-02/1")]
    c = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    assert c.execute("SELECT status, finished_at FROM run").fetchone() == ("ok", to_iso(FAKE))


def test_over_cap_raises_before_any_run_row(cli_env: Env) -> None:
    settings, profile = loaded(cli_env, cap=1000)
    spend(cli_env[1], 1000)
    with pytest.raises(BudgetRefused, match="refused"):
        with metered_run(settings, profile, "probe", "deep", force=True, clock=lambda: FAKE):
            pytest.fail("a refused run must not start")
    assert len(run_rows(cli_env[1])) == 1  # only the seeded row


def test_warning_reaches_warn_and_quick_runs_past_the_cap_unless_strict(cli_env: Env) -> None:
    settings, profile = loaded(cli_env, cap=1000)
    spend(cli_env[1], 1000)
    seen: list[str] = []
    with metered_run(
        settings, profile, "probe", "quick", force=False, clock=lambda: FAKE, warn=seen.append
    ) as m:
        m.status = "ok"
    assert len(seen) == 1 and "cap reached" in seen[0]
    with pytest.raises(BudgetRefused, match="stop at the cap"):
        with metered_run(
            settings, profile, "probe", "quick", force=False, clock=lambda: FAKE, strict=True
        ):
            pytest.fail("an unattended run must stop at the cap")
    assert len(run_rows(cli_env[1])) == 2


def test_strict_runs_below_the_cap_and_force_path_is_unchanged(cli_env: Env) -> None:
    settings, profile = loaded(cli_env, cap=1000)
    spend(cli_env[1], 500)
    with metered_run(
        settings, profile, "probe", "quick", force=False, clock=lambda: FAKE, strict=True
    ) as m:
        m.status = "ok"
    assert m.tier == "quick" and m.run_dir.parts[-2] == "2026-01-02"
    spend(cli_env[1], 350)  # 85 percent: deep is downgraded unless forced
    seen: list[str] = []
    with metered_run(
        settings, profile, "probe", "deep", force=False, clock=lambda: FAKE, warn=seen.append
    ) as m2:
        m2.status = "ok"
    assert m2.tier == "quick" and "downgraded" in seen[0]


def test_a_store_error_is_not_a_budget_refusal(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, profile = loaded(cli_env)

    def boom(_: Path) -> None:
        raise NiveshError("store busy")

    monkeypatch.setattr(rs, "init_stores", boom)
    with pytest.raises(NiveshError, match="store busy") as e:
        with metered_run(settings, profile, "probe", "quick", force=False):
            pass
    assert not isinstance(e.value, BudgetRefused)


def test_the_cli_wrappers_read_utcnow_at_call_time_and_translate_errors(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, profile = loaded(cli_env, cap=1000)
    monkeypatch.setattr(common, "utcnow", lambda: FAKE)
    with common.plain_run(settings, "probe") as m:
        m.status = "ok"
    assert m.run_dir.parts[-2] == "2026-01-02"
    spend(cli_env[1], 1000)
    with pytest.raises(typer.Exit):
        with common.metered_run(settings, profile, "probe", "deep", force=True):
            pytest.fail("refused")
    assert "refused" in capsys.readouterr().err

    def boom(_: Path) -> None:
        raise NiveshError("store busy")

    monkeypatch.setattr(rs, "init_stores", boom)
    with pytest.raises(typer.Exit):
        with common.plain_run(settings, "probe"):
            pytest.fail("no run")
    assert "error: store busy" in capsys.readouterr().err


def test_open_read_stores_reports_a_busy_market_store(cli_env: Env) -> None:
    settings, _ = loaded(cli_env)
    init_stores(cli_env[1])
    with open_read_stores(settings) as st:
        assert st.sql.execute("PRAGMA query_only").fetchone() == (1,)
        st.duck.execute("SELECT 1")
    writer = open_duck(cli_env[1] / "nivesh.duckdb")
    try:
        with pytest.raises(NiveshError, match="in use"):
            with open_read_stores(settings):
                pass
    finally:
        writer.close()


def test_held_securities_is_empty_on_a_fresh_store(cli_env: Env) -> None:
    settings, _ = loaded(cli_env)
    with open_read_stores(settings) as st:
        assert held_securities(st.sql) == []
