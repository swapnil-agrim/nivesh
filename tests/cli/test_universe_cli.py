"""`nivesh universe load | show` and `screen --universe` over a synthetic store."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from tests.cli.test_engine_cli import snapshot
from tests.ideas_fx import ASOF, IN_SYMBOLS, seed_ideas_store, with_benchmarks, write_constituents

runner = CliRunner()
Env = tuple[list[str], Path]
RULES = """\
name: demo
rules:
  - {id: trend, metric: price_vs_sma200_pct, op: ">", value: -100}
"""


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1], load=False)
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


@pytest.fixture
def loaded(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def test_universe_load_reports_loaded_and_unresolved_counts(env: Env, tmp_path: Path) -> None:
    f = write_constituents(tmp_path / "n.csv", (*IN_SYMBOLS, "ZZZ"))
    r = call(env, "universe", "load", "--index", "NIFTY500", "--file", str(f))
    assert r.exit_code == 0, r.output
    assert f"loaded 6 of 7 rows for NIFTY500 as of {ASOF}" in r.output
    assert "unresolved 1: ZZZ" in r.output
    assert "(Tech|Banks|Energy|Health)" not in r.output


def test_universe_load_unknown_index_exits_1(env: Env, tmp_path: Path) -> None:
    f = write_constituents(tmp_path / "n.csv", IN_SYMBOLS)
    r = call(env, "universe", "load", "--index", "NOPE", "--file", str(f))
    assert r.exit_code == 1 and "unknown index 'NOPE'" in r.output and "NIFTY500" in r.output
    r = call(env, "universe", "load", "--index", "NIFTY500", "--file", str(tmp_path / "gone.csv"))
    assert r.exit_code == 1 and "cannot read" in r.output
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker\nAAA\n")
    r = call(env, "universe", "load", "--index", "NIFTY500", "--file", str(bad))
    assert r.exit_code == 1 and "`symbol` column" in r.output


def test_universe_show_lists_counts_asof_and_exclusion_reasons(
    loaded: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = call(loaded, "universe", "show")
    assert r.exit_code == 0, r.output
    assert f"universe as of {ASOF}" in r.output
    assert "IN: universe IN (NIFTY500): 6 securities of 6 members" in r.output
    assert "US: universe US (NASDAQ100, SP500): 6 securities of 6 members" in r.output
    j = json.loads(call(loaded, "universe", "show", "--json").stdout)
    assert j["markets"]["IN"]["indices"]["NIFTY500"] == {"as_of": ASOF.isoformat(), "members": 6}
    assert j["markets"]["IN"]["size"] == 6 and j["as_of"] == ASOF.isoformat()


def test_universe_show_reports_an_unloaded_market_without_failing(env: Env) -> None:
    r = call(env, "universe", "show")
    assert r.exit_code == 0
    assert "IN: no members loaded for NIFTY500; run `nivesh universe load`" in r.output


def test_screen_universe_option_runs_through_the_screener(loaded: Env) -> None:
    r = call(loaded, "screen", "--rules", str(_rules(loaded)), "--universe", "india", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)["result"]
    assert out["screen"]["evaluated"] == 6
    assert out["screen"]["universe_basis"].startswith(
        "universe IN (NIFTY500): 6 securities of 6 members"
    )
    assert len(out["screen"]["matches"]) == 6
    r = call(loaded, "screen", "--rules", str(_rules(loaded)), "--universe", "us")
    assert r.exit_code == 0 and "universe US" in r.output


def _rules(env: Env) -> Path:
    p = Path(env[0][1]).parent / "rules.yaml"
    p.write_text(RULES)
    return p


def test_screen_universe_rejects_a_mix_and_an_unknown_name(loaded: Env) -> None:
    rules = str(_rules(loaded))
    r = call(loaded, "screen", "--rules", rules, "--universe", "india", "--security", "AAA")
    assert r.exit_code == 1 and "alternatives" in r.output
    r = call(loaded, "screen", "--rules", rules, "--universe", "mars")
    assert r.exit_code == 1 and "must be one of india, us" in r.output


def test_screen_universe_with_nothing_loaded_exits_1_and_does_not_widen(env: Env) -> None:
    r = call(env, "screen", "--rules", str(_rules(env)), "--universe", "india")
    assert r.exit_code == 1 and "no members loaded for NIFTY500" in r.output


def test_rs_percentile_is_universe_relative_and_explicit_path_values_unchanged(
    loaded: Env,
) -> None:
    rules = Path(loaded[0][1]).parent / "rs.yaml"
    rules.write_text("name: rs\nrules:\n  - {id: rs, metric: rs_percentile, op: '>=', value: 0}\n")

    def values(*extra: str) -> dict[str, str]:
        r = call(loaded, "screen", "--rules", str(rules), "--json", *extra)
        assert r.exit_code == 0, r.output
        m = json.loads(r.stdout)["result"]["screen"]["matches"]
        return {x["symbol"]: x["values"][0]["value"] for x in m}

    named = values("--universe", "india")
    pair = values("--security", "AAA", "--security", "BBB")
    assert set(named) == set(IN_SYMBOLS) and set(pair) == {"AAA", "BBB"}
    assert named["AAA"] != pair["AAA"] or named["BBB"] != pair["BBB"]  # cohort changes the rank
    assert values("--security", "AAA", "--security", "BBB") == pair  # explicit path is stable
    one = call(loaded, "screen", "--rules", str(rules), "--json", "--security", "AAA")
    assert json.loads(one.stdout)["result"]["requested_basis"].startswith("explicit list of 1")


def test_universe_commands_open_stores_read_only_except_load(loaded: Env, tmp_path: Path) -> None:
    before = snapshot(loaded[1])
    call(loaded, "universe", "show")
    call(loaded, "screen", "--rules", str(_rules(loaded)), "--universe", "india")
    assert snapshot(loaded[1]) == before
    f = write_constituents(tmp_path / "n.csv", IN_SYMBOLS[:3])
    assert call(loaded, "universe", "load", "--index", "NIFTY500", "--file", str(f)).exit_code == 0
    assert snapshot(loaded[1]) != before
