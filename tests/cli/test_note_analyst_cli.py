"""`nivesh ta|fa --note --analyst`: one quick analyst call added to the engine note."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import FakeSDK
from tests.ideas_fx import ASOF, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def install(monkeypatch: pytest.MonkeyPatch, **replace: Any) -> FakeSDK:
    sdk: FakeSDK = fx.happy(**replace)
    monkeypatch.setattr(runtime, "query", sdk)
    return sdk


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def status(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, status, tier FROM run").fetchall()
    finally:
        c.close()


def test_ta_analyst_flag_runs_exactly_one_quick_analyst_call(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = install(monkeypatch)
    r = call(env, "ta", "AAA", "--note", "--analyst")
    assert r.exit_code == 0, r.output
    assert [c.agent for c in sdk.seen] == ["technical"]
    assert status(env) == [("ta", "ok", "quick")]


def test_ta_note_includes_the_analyst_view_and_stays_validated(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    r = call(env, "ta", "AAA", "--note", "--analyst")
    assert "## Analyst view" in r.stdout and "A short synthetic thesis." in r.stdout
    assert "DRAFT" not in r.stdout and status(env)[0][1] == "ok"
    assert (run_path(env[1], 1) / "report.md").is_file()


def test_fa_analyst_flag_uses_the_fundamental_analyst(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = install(monkeypatch)
    assert call(env, "fa", "AAA", "--note", "--analyst").exit_code == 0
    assert [c.agent for c in sdk.seen] == ["fundamental"]


def test_analyst_failure_falls_back_to_the_engine_note_with_a_gap_line(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, technical=lambda c: RuntimeError("down"))
    r = call(env, "ta", "AAA", "--note", "--analyst")
    assert r.exit_code == 0, r.output
    assert "analyst view unavailable" in r.stdout and "## Analyst view" not in r.stdout
    assert "# Technical note: Example AAA" in r.stdout


def test_analyst_without_note_is_a_usage_error_and_spends_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = install(monkeypatch)
    assert call(env, "ta", "AAA", "--analyst").exit_code == 2
    assert sdk.seen == [] and status(env) == []
