"""`nivesh brief`: deterministic, free, one page."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_adapters.report_templates import BRIEF_WORDS, prose_words
from nivesh_agents import runtime
from nivesh_cli.main import app
from tests.ideas_fx import ASOF, hold, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("brief made a model call")

    monkeypatch.setattr(runtime, "query", boom)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def runs(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, status, cost_inr FROM run").fetchall()
    finally:
        c.close()


def test_brief_defaults_to_both(env: Env) -> None:
    r = call(env, "brief")
    assert r.exit_code == 0, r.output
    assert "# Market brief: both" in r.stdout and "| India |" in r.stdout and "| US |" in r.stdout


def test_brief_makes_no_model_call_and_costs_nothing(env: Env) -> None:
    assert call(env, "brief", "india").exit_code == 0
    assert runs(env) == [("brief", "ok", 0.0)]


def test_brief_saves_report_and_fits_400_words_plus_one_table(env: Env) -> None:
    hold(env[1], 0)
    r = call(env, "brief")
    assert r.exit_code == 0, r.output
    d = run_path(env[1], 1)
    md = (d / "report.md").read_text()
    assert prose_words(md) <= BRIEF_WORDS
    assert sum(1 for x in md.splitlines() if x.startswith("| ---")) == 1  # one table
    for name in ("report.md", "report.html", "report_input.json", "report.json"):
        assert (d / name).is_file()


def test_brief_numbers_all_validated(env: Env) -> None:
    r = call(env, "brief")
    assert "DRAFT" not in r.output and runs(env)[0][1] == "ok"


def test_brief_json(env: Env) -> None:
    r = call(env, "brief", "us", "--json")
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert r.stdout.strip() == json.dumps(data, sort_keys=True, ensure_ascii=False)
    assert data["title"] == "Market brief: us"


def test_brief_unknown_market_exits_2(env: Env) -> None:
    assert call(env, "brief", "mars").exit_code == 2 and runs(env) == []
