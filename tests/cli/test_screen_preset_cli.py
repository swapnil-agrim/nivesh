import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from tests.ideas_fx import ASOF, seed_ideas_store, with_benchmarks

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def test_screen_preset_with_universe_prints_matches_and_basis(env: Env) -> None:
    r = call(env, "screen", "--preset", "lt-quality-value", "--universe", "india", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["subject"] == "lt-quality-value"
    assert out["result"]["screen"]["universe_basis"].startswith("universe IN (NIFTY500)")
    assert out["result"]["screen"]["evaluated"] == 6
    text = call(env, "screen", "--preset", "lt-quality-value", "--universe", "us")
    assert text.exit_code == 0 and "universe US" in text.output


def test_unknown_preset_lists_available_names_and_exits_1(env: Env) -> None:
    r = call(env, "screen", "--preset", "nope")
    assert r.exit_code == 1
    assert "unknown preset 'nope'; available: lt-quality-growth, lt-quality-value" in r.output


def test_preset_and_rules_file_are_mutually_exclusive(env: Env, tmp_path: Path) -> None:
    f = tmp_path / "r.yaml"
    f.write_text("rules:\n  - {id: a, metric: roce, op: '>', value: 1}\n")
    both = call(env, "screen", "--preset", "pos-breakout", "--rules", str(f))
    assert both.exit_code == 1 and "exactly one of --rules FILE and --preset NAME" in both.output
    neither = call(env, "screen")
    assert neither.exit_code == 1 and "exactly one" in neither.output


def test_a_path_is_not_a_preset_name(env: Env) -> None:
    r = call(env, "screen", "--preset", "../profile")
    assert r.exit_code == 1 and "unknown preset" in r.output
