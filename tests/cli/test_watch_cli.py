import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
import nivesh_cli.main as cmain
from nivesh_cli.main import app
from nivesh_core.db.duck import open_duck
from nivesh_core.ledger import LedgerEntry, record_call
from nivesh_core.watch import watched
from tests.cli.test_engine_cli import snapshot
from tests.ideas_fx import ASOF, seed_ideas_store

runner = CliRunner()
Env = tuple[list[str], Path]
D = Decimal
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def last_close(env: Env, symbol: str) -> Decimal:
    sql = sqlite3.connect(env[1] / "nivesh.sqlite")
    sid = sql.execute("SELECT id FROM security WHERE symbol = ?", (symbol,)).fetchone()[0]
    sql.close()
    duck = open_duck(env[1] / "nivesh.duckdb", read_only=True)
    try:
        row = duck.execute(
            "SELECT close FROM price_bar WHERE security_id = ? ORDER BY date DESC LIMIT 1", (sid,)
        ).fetchone()
    finally:
        duck.close()
    return D(str(row[0]))


def zone_around(close: Decimal, lo: str, hi: str) -> tuple[str, str]:
    """A zone `lo`/`hi` times the close, as text with two decimals."""
    return (format((close * D(lo)).quantize(D("0.01")), "f"),
            format((close * D(hi)).quantize(D("0.01")), "f"))  # fmt: skip


def test_watch_add_ticker_with_zone_stores_it(env: Env) -> None:
    r = call(env, "watch", "add", "AAA", "90.5", "100")
    assert r.exit_code == 0, r.output
    assert "tracking AAA (IN), entry zone 90.5 to 100" in r.output
    sql = sqlite3.connect(env[1] / "nivesh.sqlite")
    (w,) = watched(sql)
    sql.close()
    assert (w.entry_low, w.entry_high, w.added_on) == (D("90.5"), D("100"), ASOF)
    again = call(env, "watch", "add", "AAA", "80", "95")
    assert again.exit_code == 0
    sql = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert [x.entry_low for x in watched(sql)] == [D(80)]  # replaced, not a second row
    sql.close()


def test_watch_add_without_zone_allowed(env: Env) -> None:
    r = call(env, "watch", "add", "BBB")
    assert r.exit_code == 0 and "no entry zone" in r.output
    half = call(env, "watch", "add", "BBB", "90")
    assert half.exit_code == 1 and "both ends" in half.output
    bad = call(env, "watch", "add", "BBB", "x", "y")
    assert bad.exit_code == 1 and "LOW must be a number" in bad.output
    flipped = call(env, "watch", "add", "BBB", "100", "90")
    assert flipped.exit_code == 1 and "low 100 is above high 90" in flipped.output


def test_watch_add_unknown_ticker_exits_1(env: Env) -> None:
    r = call(env, "watch", "add", "NOPE1", "1", "2")
    assert r.exit_code == 1 and "no security matches 'NOPE1'" in r.output


def test_watch_add_ambiguous_ticker_asks_for_exchange_or_isin(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ideas_store(cli_env[1], in_symbols=("DUP", "BBB"), us_symbols=("DUP", "UBB"), load=False)
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    r = call(cli_env, "watch", "add", "DUP", "1", "2")
    assert r.exit_code == 1
    assert "ambiguous across exchanges or markets" in r.output and "give the ISIN" in r.output
    sql = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    sid = sql.execute("SELECT id FROM security WHERE symbol = 'DUP' AND market = 'US'").fetchone()[
        0
    ]
    sql.close()
    ok = call(cli_env, "watch", "add", f"id:{sid}", "1", "2")
    assert ok.exit_code == 0 and "tracking DUP (US)" in ok.output


def record(env: Env, symbol: str, verdict: str) -> None:
    sql = sqlite3.connect(env[1] / "nivesh.sqlite", isolation_level=None)
    sid = sql.execute("SELECT id FROM security WHERE symbol = ?", (symbol,)).fetchone()[0]
    sql.execute("INSERT INTO run (id, command, started_at, status) VALUES (7, 'ideas', 't', 'ok')")
    from datetime import date

    record_call(
        sql,
        LedgerEntry(
            run_id=7, security_id=sid, verdict=verdict, horizon="long_term_1y_plus",
            conviction="high", review_date=date(2026, 4, 2), benchmark_reason="none",
            input_hash="h", prompt_versions={}, model_versions={}, reported=True,
        ),
    )  # fmt: skip
    sql.close()


def test_watch_shows_distance_and_latest_verdict_from_ledger(env: Env) -> None:
    close = last_close(env, "AAA")
    lo, hi = zone_around(close, "0.70", "0.80")  # the close sits above the zone
    assert call(env, "watch", "add", "AAA", lo, hi).exit_code == 0
    record(env, "AAA", "ACCUMULATE")
    r = call(env, "watch")
    assert r.exit_code == 0, r.output
    line = next(x for x in r.output.splitlines() if x.startswith("AAA"))
    assert f"zone {lo} to {hi}" in line and f"close {close}" in line
    pct = ((close - D(hi)) / D(hi) * 100).quantize(D("0.0001"))
    assert f"{pct}% above the entry zone" in line
    assert "latest verdict: ACCUMULATE (high, run 7)" in line
    assert r.output.startswith(f"watch as of {ASOF}")


def test_watch_inside_below_and_no_zone_cases(env: Env) -> None:
    close = last_close(env, "BBB")
    inside = zone_around(close, "0.90", "1.10")
    below = zone_around(close, "1.20", "1.30")
    call(env, "watch", "add", "BBB", *inside)
    call(env, "watch", "add", "CCC", *below)
    call(env, "watch", "add", "DDD")
    out = call(env, "watch").output
    assert "BBB (IN)" in out and "inside the entry zone (0%)" in out
    assert "% below the entry zone" in next(x for x in out.splitlines() if x.startswith("CCC"))
    assert "distance n/a (no entry zone set)" in next(
        x for x in out.splitlines() if x.startswith("DDD")
    )


def test_watch_without_verdict_says_no_verdict_yet(env: Env) -> None:
    call(env, "watch", "add", "AAA", "1", "2")
    assert "latest verdict: no verdict yet" in call(env, "watch").output


def test_watch_json(env: Env) -> None:
    close = last_close(env, "AAA")
    lo, hi = zone_around(close, "0.70", "0.80")
    call(env, "watch", "add", "AAA", lo, hi)
    record(env, "AAA", "BUY")
    r = call(env, "watch", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert r.stdout.strip() == json.dumps(data, sort_keys=True)
    (w,) = data["watched"]
    assert w["symbol"] == "AAA" and w["entry_low"] == lo and w["last_close"] == str(close)
    assert w["distance"]["state"] == "above" and D(w["distance"]["percent"]) > 0
    assert w["verdict"]["verdict"] == "BUY" and data["command"] == "watch"


def test_watch_empty_says_nothing_tracked(env: Env) -> None:
    r = call(env, "watch")
    assert r.exit_code == 0 and "nothing tracked yet" in r.output
    assert json.loads(call(env, "watch", "--json").stdout)["watched"] == []


def test_watch_list_opens_store_read_only(env: Env) -> None:
    call(env, "watch", "add", "AAA", "1", "2")
    before = snapshot(env[1])
    call(env, "watch")
    call(env, "watch", "--json")
    assert snapshot(env[1]) == before


def test_no_alert_or_notification_code_path() -> None:
    names = [p.name for p in (ROOT / "nivesh_core").glob("*.py")]
    names += [p.name for p in (ROOT / "nivesh_cli").glob("*.py")]
    assert not [n for n in names if any(w in n for w in ("alert", "notif", "notify", "push"))]
    for module in ("nivesh_cli/watch.py", "nivesh_core/watch.py"):
        text = (ROOT / module).read_text().lower()
        assert not any(w in text for w in ("smtp", "webhook", "slack", "sendmail", "notify("))
    assert cmain.watch_app is not None
