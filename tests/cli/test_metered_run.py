"""The shared run lifecycle (gate, run row, trace) used by `nivesh run` and the review commands."""

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from nivesh_agents import runtime
from nivesh_cli.common import metered_run
from nivesh_cli.main import app
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.profile import Profile, load_profile
from nivesh_core.timeutil import to_iso, utcnow
from nivesh_core.trace import read_trace
from tests.run_paths import run_col, run_path

Env = tuple[list[str], Path]


def loaded(env: Env, cap: int | None = None) -> tuple[Settings, Profile]:
    cfg = Path(env[0][1])
    if cap is not None:
        p = cfg / "profile.yaml"
        p.write_text(p.read_text().replace("monthly_cost_cap: 2000", f"monthly_cost_cap: {cap}"))
    return load_settings(cfg / "nivesh.yaml"), load_profile(cfg / "profile.yaml")


def rows(data: Path) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(data / "nivesh.sqlite")
    try:
        return c.execute(
            "select command, status, tier, model, input_tokens, cost_inr, run_dir, prompt_version"
            " from run order by id"
        ).fetchall()
    finally:
        c.close()


def test_metered_run_records_a_run_row_and_finishes_it_ok_or_error(cli_env: Env) -> None:
    settings, profile = loaded(cli_env)
    with metered_run(settings, profile, "probe", "quick", force=False) as m:
        assert m.run_dir.is_dir() and m.run_id == 1 and m.tier == "quick"
        m.tracer.start("probe", "v1")
        m.tracer.finish_run("claude-test")
        m.status = "ok"
    with pytest.raises(RuntimeError), metered_run(settings, profile, "probe", "quick", force=False):
        raise RuntimeError("boom")
    got = rows(cli_env[1])
    assert got[0][:6] == ("probe", "ok", "quick", "claude-test", 0, 0.0) and got[0][7] == "v1"
    assert re.fullmatch(run_col(1), got[0][6])
    assert got[1][:3] == ("probe", "error", "quick") and re.fullmatch(run_col(2), got[1][6])


def test_metered_run_respects_the_cost_gate_and_exits_1_when_blocked(
    cli_env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, profile = loaded(cli_env, cap=1000)
    init_stores(cli_env[1])
    c = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    c.execute(
        "insert into run (command, started_at, status, cost_inr) values ('x', ?, 'ok', 1000)",
        (to_iso(utcnow()),),
    )
    c.commit()
    c.close()
    with pytest.raises(typer.Exit), metered_run(settings, profile, "probe", "deep", force=True):
        pytest.fail("a refused run must not start")
    assert "refused" in capsys.readouterr().err
    assert len(rows(cli_env[1])) == 1  # no new run row
    with metered_run(settings, profile, "probe", "quick", force=False) as m:
        m.status = "ok"  # quick is warned past the cap, not refused
    assert "cap reached" in capsys.readouterr().err


def test_run_command_behaviour_is_unchanged(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.agents.test_runtime import fake_query, full_run

    monkeypatch.setattr(runtime, "query", fake_query(*full_run()))
    r = CliRunner().invoke(app, [*cli_env[0], "run", "ping"])
    assert r.exit_code == 0 and "pong" in r.output
    ((command, status, tier, model, tok, cost, rdir, ver),) = rows(cli_env[1])
    assert (command, status, tier, model, tok) == ("ping", "ok", "quick", "claude-test", 100)
    assert re.fullmatch(run_col(1), rdir)
    assert cost > 0 and ver is not None and len(ver) == 12
    recs = read_trace(run_path(cli_env[1], 1) / "trace.jsonl")
    assert [x["type"] for x in recs] == [
        "start", "assistant_text", "tool_call", "tool_result", "result",
    ]  # fmt: skip
