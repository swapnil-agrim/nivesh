"""`decide`, `rescore`, `scorecard`, `calibration` over synthetic stores (no network or model)."""

import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from nivesh_core.ledger import LedgerEntry, effective_decisions, record_call
from tests.ideas_fx import with_benchmarks
from tests.scoring_fx import Fx, line_bars, make_fx

runner = CliRunner()
Env = tuple[list[str], Path]
D = Decimal


FEB3 = date(2026, 2, 3)


@pytest.fixture
def fx(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fx]:
    f = make_fx(cli_env[1].parent)
    f.sql.close()  # the CLI opens its own connections
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: FEB3)
    yield f
    f.duck.close()


@pytest.fixture
def env(cli_env: Env, fx: Fx) -> Env:
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def run_cli(env: Env, fx: Fx, *args: str) -> Any:
    fx.duck.close()  # one writer at a time: the command reads the market store
    return call(env, *args)


def sql_of(env: Env) -> sqlite3.Connection:
    return sqlite3.connect(env[1] / "nivesh.sqlite", isolation_level=None)


def add_entry(
    env: Env, symbol: str = "AAA", verdict: str = "SELL", run_id: int = 7, **kw: Any
) -> int:
    c = sql_of(env)
    sid = c.execute("SELECT id FROM security WHERE symbol = ?", (symbol,)).fetchone()[0]
    if not c.execute("SELECT 1 FROM run WHERE id = ?", (run_id,)).fetchone():
        c.execute(
            "INSERT INTO run (id, command, started_at, status) VALUES (?, 'ideas', 't', 'ok')",
            (run_id,),
        )
    base: dict[str, Any] = dict(
        run_id=run_id, security_id=sid, verdict=verdict, horizon="long_term_1y_plus",
        conviction="high", review_date=date(2026, 4, 2), benchmark_reason="none",
        input_hash="h", prompt_versions={}, model_versions={}, reported=True,
    )  # fmt: skip
    base.update(kw)
    eid = record_call(c, LedgerEntry(**base))
    c.close()
    return eid


def test_decide_records_a_row_and_prints_it(env: Env) -> None:
    eid = add_entry(env)
    r = call(
        env, "decide", str(eid), "acted", "--qty", "10", "--price", "100.5", "--on", "2025-12-31"
    )
    assert r.exit_code == 0, r.output
    assert (
        f"decision 1 recorded: acted on entry {eid} qty 10 price 100.5 INR on 2025-12-31"
        in r.output
    )
    fix = call(env, "decide", str(eid), "acted", "--qty", "4", "--price", "100", "--corrects", "1")
    assert fix.exit_code == 0 and "(replaces decision 1)" in fix.output
    c = sql_of(env)
    assert [r.id for r in effective_decisions(c)[eid]] == [2]
    assert c.execute("SELECT count(*) FROM ledger_decision").fetchone() == (2,)
    c.close()


def test_decide_errors_exit_one_and_store_nothing(env: Env) -> None:
    eid = add_entry(env)
    for args, msg in (
        (["999", "skipped"], "no ledger entry 999"),
        ([str(eid), "acted"], "positive quantity and price"),
        ([str(eid), "acted", "--qty", "x", "--price", "1"], "--qty must be a number"),
        ([str(eid), "skipped", "--on", "soon"], "--on must be an ISO date"),
        ([str(eid), "skipped", "--on", "2999-01-01"], "future"),
        ([str(eid), "skipped", "--currency", "USD"], "does not match"),
        ([str(eid), "skipped", "--note", "ring 98765 43210 or a@b.co"], "personal data"),
        ([str(eid), "skipped", "--corrects", "5"], "not a decision"),
    ):
        r = call(env, "decide", *args)
        assert r.exit_code == 1 and msg in r.output, (args, r.output)
    c = sql_of(env)
    assert c.execute("SELECT count(*) FROM ledger_decision").fetchone() == (0,)
    c.close()


def test_decide_has_no_option_that_edits_an_old_row(env: Env) -> None:
    cmd = typer.main.get_command(app).commands["decide"]  # type: ignore[attr-defined]
    opts = {o for p in cmd.params for o in p.opts}  # registered names, not rich-wrapped help text
    assert "--corrects" in opts and not opts & {"--update", "--edit"}


def load_line(fx: Fx) -> None:
    line_bars(fx, "AAA", date(2026, 1, 1), FEB3)
    line_bars(fx, "BENCHIN", date(2026, 1, 1), FEB3, start=200)


def issue(env: Env, symbol: str = "AAA", verdict: str = "BUY") -> int:
    return add_entry(
        env, symbol, verdict, as_of=date(2026, 1, 5), price_date=date(2026, 1, 5), run_id=1
    )


def test_rescore_prints_counts_and_the_basis_note_then_adds_nothing(env: Env, fx: Fx) -> None:
    issue(env)
    issue(env, verdict="SELL")
    load_line(fx)
    r = run_cli(env, fx, "rescore")
    assert r.exit_code == 0, r.output
    assert "scored 4, insufficient 0, pending 6 (from 2 calls)" in r.output
    assert "price index, not TRI" in r.output
    again = call(env, "rescore")
    assert again.exit_code == 0 and "scored 0, insufficient 0, pending 6" in again.output
    js = json.loads(call(env, "rescore", "--json").stdout)
    assert js["scored"] == 0 and js["pending"] == 6 and "not TRI" in js["basis"]


def test_rescore_with_an_empty_ledger_says_nothing_to_score(env: Env, fx: Fx) -> None:
    r = run_cli(env, fx, "rescore")
    assert r.exit_code == 0 and "nothing to score" in r.output


def test_rescore_reports_a_busy_market_database(env: Env, fx: Fx) -> None:
    issue(env)
    r = call(env, "rescore")  # the fixture still holds the writer
    assert r.exit_code == 1 and "in use by another process" in r.output


def test_command_names_are_unique_and_score_is_still_the_factor_score() -> None:
    group = typer.main.get_command(app)
    names = [
        c.name or typer.main.get_command_name(c.callback.__name__)  # type: ignore[union-attr]
        for c in app.registered_commands
    ]
    assert len(names) == len(set(names))
    cmds = group.commands  # type: ignore[attr-defined]
    assert {"decide", "rescore", "score"} <= set(cmds)
    assert cmds["score"].callback.__module__ == "nivesh_cli.engine"
    assert cmds["rescore"].callback.__module__ == "nivesh_cli.scorecard"


def test_commands_never_open_the_network(env: Env, fx: Fx, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*a: Any, **k: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", deny)
    issue(env)
    load_line(fx)
    assert run_cli(env, fx, "rescore").exit_code == 0


def test_scorecard_prints_buckets_basis_and_saves_a_report(env: Env, fx: Fx) -> None:
    issue(env)
    issue(env, verdict="SELL")
    load_line(fx)
    assert run_cli(env, fx, "rescore").exit_code == 0
    r = call(env, "scorecard")
    assert r.exit_code == 0, r.output
    assert "# Scorecard" in r.stdout and "price index, not TRI" in r.stdout
    assert "- BUY: 1 calls, 0 scored, not enough data" in r.stdout  # 13 weeks is not due yet
    assert "| 1 week | 2 | 2 | not enough data |" in r.stdout
    assert "waiting for price data (pending)" in r.stdout
    assert "saved:" in r.stderr
    runs = sorted((env[1] / "runs").rglob("report.md"))
    assert runs and "price index, not TRI" in runs[-1].read_text()


def test_scorecard_since_filters_and_json_is_valid(env: Env, fx: Fx) -> None:
    issue(env)
    fx.duck.close()
    r = call(env, "scorecard", "--since", "2026-01-06", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["title"] == "Scorecard"
    assert "Not enough data yet" in json.dumps(out)
    bad = call(env, "scorecard", "--since", "soon")
    assert bad.exit_code == 1 and "--since must be an ISO date" in bad.output


def test_scorecard_on_an_empty_ledger_prints_the_empty_state(env: Env, fx: Fx) -> None:
    fx.duck.close()
    r = call(env, "scorecard")
    assert r.exit_code == 0 and "Not enough data yet" in r.stdout


def test_scorecard_marks_calls_acted_on(env: Env, fx: Fx) -> None:
    eid = issue(env)
    assert call(env, "decide", str(eid), "acted", "--qty", "1", "--price", "100").exit_code == 0
    fx.duck.close()
    r = call(env, "scorecard")
    assert "- acted: 1 calls" in r.stdout and "- system: 0" not in r.stdout


def seed_scored(env: Env, count: int, hit_for: Any) -> None:
    """`count` distinct calls (different security, verdict or a 100-day gap) scored at 13 weeks;
    `hit_for(conviction, n)` says whether call n is a hit."""
    from datetime import timedelta

    from nivesh_core.ledger import ScoreRow, record_scores

    verdicts = ("BUY", "ACCUMULATE", "SELL", "TRIM", "AVOID")
    made = 0
    rows = []
    for gap in range(10):
        for symbol in ("AAA", "BBB", "UUU"):
            for verdict in verdicts:
                if made == count:
                    break
                conviction = ("high", "medium", "low")[made % 3]
                eid = add_entry(
                    env, symbol, verdict, run_id=1, conviction=conviction,
                    as_of=date(2026, 1, 5) + timedelta(days=100 * gap), price_date=None,
                )  # fmt: skip
                hit = bool(hit_for(conviction, made))
                rows.append(
                    ScoreRow(
                        entry_id=eid,
                        horizon_weeks=13,
                        status="scored",
                        ret=D(1),
                        benchmark_ret=D(0),
                        excess=D(1),
                        call_excess=D(2) if hit else D(-1),
                        hit=hit,
                        basis="split_adjusted_price",
                    )  # fmt: skip
                )
                made += 1
    c = sql_of(env)
    record_scores(c, rows)
    c.close()


def test_calibration_below_the_floor_says_not_enough_data(env: Env, fx: Fx) -> None:
    seed_scored(env, 12, lambda lv, n: True)
    fx.duck.close()
    r = call(env, "calibration")
    assert r.exit_code == 0 and "not enough data (12 of 40 scored calls)" in r.output
    assert "price index, not TRI" in r.output
    assert call(env, "calibration", "--json").exit_code == 0


def test_calibration_with_enough_calls_prints_buckets_and_a_verdict(env: Env, fx: Fx) -> None:
    rate = {"high": 9, "medium": 5, "low": 2}  # hits out of every 10 calls
    seed_scored(env, 45, lambda lv, n: (n // 3) % 10 < rate[lv])
    fx.duck.close()
    r = call(env, "calibration")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0] == "calibration at 13 weeks, 45 scored calls"
    assert [x.split()[0] for x in lines[1:4]] == ["high", "medium", "low"]
    assert "hit rate" in lines[1] and "90% interval" in lines[1]
    assert lines[4].startswith("verdict: ") and "basis: price index, not TRI" in lines[5]
    a = json.loads(call(env, "calibration", "--json").stdout)
    b = json.loads(call(env, "calibration", "--json").stdout)
    assert a == b and a["n"] == 45 and a["enough"] is True and len(a["buckets"]) == 3
    assert list(a) == sorted(a)  # keys sorted
