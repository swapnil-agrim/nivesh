"""`nivesh ta|fa --note`: a saved, checked engine note; plain `ta`/`fa` output is untouched."""

import sqlite3

import pytest

from tests.cli import test_engine_cli as tec
from tests.cli.test_engine_cli import ASOF_TEXT, Env, call
from tests.run_paths import run_path

env = tec.env


def runs(e: Env) -> list[tuple[str, str, float]]:
    c = sqlite3.connect(e[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, status, cost_inr FROM run").fetchall()
    finally:
        c.close()


def test_ta_note_prints_and_saves_in_the_dated_run_dir_with_zero_cost(env: Env) -> None:
    r = call(env, "ta", "US1", "--note")
    assert r.exit_code == 0, r.output
    assert "# Technical note: Example Tech 1" in r.stdout and "DRAFT" not in r.stdout
    assert runs(env) == [("ta", "ok", 0.0)]
    d = run_path(env[1], 1)
    assert {"report.md", "report.html", "report_input.json", "report.json"} <= {
        p.name for p in d.iterdir()
    }


def test_fa_note_json_and_no_model_call(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from nivesh_agents import runtime

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("a model call was made")

    monkeypatch.setattr(runtime, "query", boom)
    r = call(env, "fa", "US1", "--note", "--json")
    assert r.exit_code == 0, r.output
    assert '"title": "Fundamental note: Example Tech 1"' in r.stdout
    assert runs(env)[0][1] == "ok"


def test_ta_without_note_prints_exactly_what_it_printed_before(env: Env) -> None:
    r = call(env, "ta", "US1")
    assert r.output.startswith(f"ta US1 as of {ASOF_TEXT}") and runs(env) == []


def test_ta_ambiguous_ticker_exits_1_listing_candidates(env: Env) -> None:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    c.execute(
        "INSERT INTO security (symbol, exchange, currency, market, name) "
        "VALUES ('US1', 'NYSE', 'USD', 'US', 'Other Us One')"
    )
    c.commit()
    c.close()
    for extra in ([], ["--note"]):
        r = call(env, "ta", "US1", *extra)
        assert r.exit_code == 1 and "ambiguous" in r.output and "NSE: or US:" in r.output
    assert runs(env) == []
