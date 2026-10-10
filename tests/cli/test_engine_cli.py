"""`nivesh ta | fa | valuation | flags | xray | risk | screen | score` against real temp stores.
Nothing is fetched and nothing is written; every figure is built from integer arithmetic."""

import json
import socket
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars, write_fundamentals, write_macro
from nivesh_core.security_master import SecurityMaster, build_master
from tests.analysis_fx import annual_rows, day, lcg_bars, universe_book
from tests.holdings_fx import holding
from tests.market_fx import mrow
from tests.us_fx import lot, usd_holding

D = Decimal
runner = CliRunner()
Env = tuple[list[str], Path]
ASOF = day(299)
ASOF_TEXT = ASOF.isoformat()
IN_ISIN, US_ISIN = "INE002A01018", "US12AB34CD56"
US_COUNT = 3


def seed_cli_store(data: Path) -> None:
    """A small stored universe: one Indian share, three US shares and an index, with bars,
    statements, a USDINR series and a holding for the Indian share and the first US share."""
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        build_master(
            sql,
            [
                mrow("RELI", name="Example Energy", isin=IN_ISIN, sector="Energy", industry="Oil"),
                mrow("NIFTY 50", name="NIFTY 50", asset_class="index"),
                *(
                    mrow(
                        f"US{i}",
                        "NASDAQ",
                        name=f"Example Tech {i}",
                        market="US",
                        currency="USD",
                        sector="Tech",
                        industry="Software",
                        **({"isin": US_ISIN} if i == 1 else {}),
                    )  # fmt: skip
                    for i in range(1, US_COUNT + 1)
                ),
            ],
            [],
        )
        ids = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
        duck = open_duck(data / "nivesh.duckdb")
        try:
            for n, symbol in enumerate(["RELI", "NIFTY 50", "US1", "US2", "US3"], start=1):
                series = lcg_bars(300, seed=n)
                upsert_bars(
                    duck,
                    [PriceBar(security_id=ids[symbol], date=b.date, close=b.close,
                              high=b.high, low=b.low, volume=b.volume, source="yahoo")
                     for b in series],
                )  # fmt: skip
            for i in range(1, US_COUNT + 1):
                write_fundamentals(
                    duck, ids[f"US{i}"], annual_rows(universe_book(i), last_fy=2023), "edgar"
                )
            write_macro(duck, "usdinr", [(date(2024, 2, 1), D(80)), (ASOF, D(85))], "fred")
        finally:
            duck.close()
        save_ingest(
            sql, kind="investright", source_label="broker", digest=None, as_of=ASOF,
            holdings=[holding(isin=IN_ISIN, symbol="RELI", exchange="NSE", name="Example Energy",
                              quantity=D(10), price=D(1000), avg_cost=D(900), as_of=ASOF)],
            txns=[], holder_refs=[""], warnings=[],
        )  # fmt: skip
        usd = usd_holding(isin=US_ISIN, symbol="US1", exchange="NASDAQ", name="Example Tech 1",
                          quantity=D(10), price=D(10), avg_cost=D(8), as_of=ASOF)  # fmt: skip
        save_ingest(
            sql, kind="us_csv", source_label="us broker", digest="d", as_of=ASOF, holdings=[usd],
            txns=[], holder_refs=[""], warnings=[],
            lots=[lot(symbol="US1", acquired_on=date(2024, 2, 1), quantity=D(10),
                      cost_per_unit=D(8))],
        )  # fmt: skip
        assert SecurityMaster(sql).by_symbol("RELI")
    finally:
        sql.close()


RULES = """\
name: demo
rules:
  - {id: above_trend, metric: price_vs_sma200_pct, op: ">", value: -100}
  - {id: revised_up, metric: revisions, op: ">", value: 0}
"""


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_cli_store(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def as_json(env: Env, *args: str) -> dict[str, Any]:
    r = call(env, *args, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)  # type: ignore[no-any-return]


def rules_file(tmp_path: Path, text: str = RULES) -> str:
    p = tmp_path / "rules.yaml"
    p.write_text(text)
    return str(p)


def test_ta_prints_indicators_with_last_bar_date_and_json_flag(env: Env) -> None:
    r = call(env, "ta", "US1")
    assert r.exit_code == 0, r.output
    assert f"ta US1 as of {ASOF_TEXT}" in r.output
    assert f"last_bar_date: {ASOF_TEXT}" in r.output and "rsi_14:" in r.output
    assert "sma_200:" in r.output and "setup:" in r.output
    data = as_json(env, "ta", "US1")
    assert data["command"] == "ta" and data["subject"] == "US1"
    ind = data["result"]["indicators"]
    assert ind["last_bar_date"] == ASOF_TEXT and ind["bars_used"] == 300
    assert ind["values"]["rsi_14"]["available"] is True
    assert isinstance(ind["values"]["rsi_14"]["value"], str)  # exact decimal text
    assert ind["values"]["rs_benchmark_ratio"]["available"] is False  # no benchmark configured


def test_fa_with_as_of_excludes_later_filings(env: Env) -> None:
    early = as_json(env, "fa", "US1", "--as-of", "2024-01-31")["result"]
    late = as_json(env, "fa", "US1", "--as-of", ASOF_TEXT)["result"]
    # the 2023 annual filing came out on 15 February 2024: the early run must not see it
    assert early["data_through"] == "2023-02-15" and late["data_through"] == "2024-02-15"
    assert late["as_of"] == ASOF_TEXT and early["as_of"] == "2024-01-31"
    text = call(env, "fa", "US1")
    assert text.exit_code == 0 and "coverage_pct:" in text.output


def test_valuation_prints_multiples_percentile_and_assumptions(env: Env) -> None:
    r = call(env, "valuation", "US1")
    assert r.exit_code == 0, r.output
    for word in ("multiples:", "percentiles:", "peer_median:", "range:", "assumptions:"):
        assert word in r.output, word
    data = as_json(env, "valuation", "US1")["result"]
    assert set(data) == {"multiples", "range"}
    assert "pe" in data["multiples"]["multiples"]
    assert set(data["range"]["scenarios"]) == {"bear", "base", "bull"}


def test_flags_prints_status_severity_and_evidence(env: Env) -> None:
    r = call(env, "flags", "US1")
    assert r.exit_code == 0, r.output
    assert "pledge:" in r.output and "status:" in r.output
    assert "severity:" in r.output and "evidence:" in r.output
    flags = as_json(env, "flags", "US1")["result"]["flags"]
    assert [f["flag"] for f in flags][0] == "pledge"
    assert all(f["status"] in ("fired", "clear", "not_evaluable") for f in flags)
    pledge = flags[0]
    assert pledge["status"] == "not_evaluable" and pledge["reason"]  # US: no pledge data


def test_xray_prints_allocation_drift_concentration_and_xirr_coverage(env: Env) -> None:
    r = call(env, "xray")
    assert r.exit_code == 0, r.output
    for word in ("allocation:", "drift:", "concentration:", "total_return:", "coverage_pct:"):
        assert word in r.output, word
    data = as_json(env, "xray")["result"]["xray"]
    assert D(data["total_inr"]) == D(10000) + D(10) * D(10) * D(85)  # 10 shares at 10 USD
    assert data["included"] == 2 and set(data["allocation"]) >= {"asset_class", "market"}
    assert data["total_return"]["coverage_pct"] is not None
    assert {x["name"] for x in data["returns"]} == {"Example Energy", "Example Tech 1"}


def test_risk_prints_vol_drawdown_beta_and_days_to_trade(env: Env) -> None:
    r = call(env, "risk", "--candidate", "US2", "--weight", "5")
    assert r.exit_code == 0, r.output
    for word in ("overall_vol:", "max_drawdown_pct:", "beta:", "days_to_trade:", "pro_forma:"):
        assert word in r.output, word
    data = as_json(env, "risk")["result"]["risk"]
    assert data["pro_forma"] is None and data["holdings"]
    assert data["coverage_pct"] is not None


def test_screen_runs_a_yaml_rule_file_and_reports_skipped_rules(env: Env, tmp_path: Path) -> None:
    r = call(env, "screen", "--rules", rules_file(tmp_path))
    assert r.exit_code == 0, r.output
    assert "matches:" in r.output and "skipped:" in r.output and "revised_up" in r.output
    data = as_json(env, "screen", "--rules", rules_file(tmp_path))["result"]
    found = data["screen"]
    assert {m["symbol"] for m in found["matches"]} == {"RELI", "US1", "US2", "US3"}
    assert found["skipped"][0]["rule_id"] == "revised_up"  # no stored estimates anywhere
    assert (
        "explicit list" in found["universe_basis"] and "all 4 securities" in data["requested_basis"]
    )
    one = as_json(env, "screen", "--rules", rules_file(tmp_path), "--security", "US2")["result"]
    assert [m["symbol"] for m in one["screen"]["matches"]] == ["US2"]


def test_score_prints_factors_composite_band_cap_and_weights_version(env: Env) -> None:
    r = call(env, "score")
    assert r.exit_code == 0, r.output
    for word in ("weights_version:", "weights_digest:", "composite:", "band:", "factors:"):
        assert word in r.output, word
    data = as_json(env, "score", "--horizon", "positional")["result"]
    assert data["horizon"] == "positional" and len(data["scores"]) == 4
    card = data["scores"][0]["card"]
    assert card["weights_version"] and len(card["weights_digest"]) >= 8
    assert set(card["factors"]) == {"quality", "value", "growth", "momentum", "risk"}
    assert "cap" in card and card["band"]


def test_unknown_symbol_exit_1_with_message(env: Env) -> None:
    for cmd in ("ta", "fa", "valuation", "flags"):
        r = call(env, cmd, "NOSUCH")
        assert r.exit_code == 1, cmd
        assert "no security matches" in r.output


def test_bad_inputs_exit_1_with_message(env: Env, tmp_path: Path) -> None:
    bad = call(
        env, "screen", "--rules", rules_file(tmp_path, "rules:\n  - {id: a, metric: nope}\n")
    )
    assert bad.exit_code == 1 and "rule file" in bad.output
    missing = call(env, "screen", "--rules", str(tmp_path / "absent.yaml"))
    assert missing.exit_code == 1 and "cannot read the rule file" in missing.output
    assert call(env, "ta", "US1", "--as-of", "not-a-date").exit_code == 1
    assert call(env, "score", "--horizon", "weekly").exit_code == 1
    assert call(env, "risk", "--candidate", "US2").exit_code == 1  # a candidate needs a weight
    assert call(env, "risk", "--candidate", "US2", "--weight", "150").exit_code == 1


def test_bad_rule_file_exit_1_with_message(env: Env, tmp_path: Path) -> None:
    r = call(env, "screen", "--rules", rules_file(tmp_path, "rules: []\n"))
    assert r.exit_code == 1 and "rule file" in r.output


def reject_float(text: str) -> float:
    raise AssertionError(f"a JSON number with a fraction ({text}) where an exact string belongs")


def test_cli_json_decimals_are_exact_strings_and_output_is_byte_identical_across_runs(
    env: Env, tmp_path: Path
) -> None:
    rules = rules_file(tmp_path)
    commands = [
        ("ta", "US1"), ("fa", "US1"), ("valuation", "US1"), ("flags", "US1"), ("xray",),
        ("risk",), ("screen", "--rules", rules), ("score",),
    ]  # fmt: skip
    for args in commands:
        first = call(env, *args, "--json")
        second = call(env, *args, "--json")
        assert first.exit_code == 0, (args, first.output)
        assert first.stdout == second.stdout, args
        json.loads(first.stdout, parse_float=reject_float)


def snapshot(data: Path) -> dict[str, list[Any]]:
    """Every table of both stores: its row count and its rows in a fixed sequence."""
    out: dict[str, list[Any]] = {}
    duck = duckdb.connect(str(data / "nivesh.duckdb"), read_only=True)
    try:
        for (name,) in duck.execute("SHOW TABLES").fetchall():
            rows = duck.execute(f"SELECT * FROM {name} ORDER BY ALL").fetchall()  # noqa: S608
            out[f"duck.{name}"] = [len(rows), rows]
    finally:
        duck.close()
    sql = sqlite3.connect(str(data / "nivesh.sqlite"))
    try:
        for (name,) in sql.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            rows = sql.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall()  # noqa: S608
            out[f"sqlite.{name}"] = [len(rows), rows]
    finally:
        sql.close()
    return out


def test_commands_never_write_to_the_stores(env: Env, tmp_path: Path) -> None:
    before = snapshot(env[1])
    assert any(v[0] for v in before.values())  # control: the stores hold data
    rules = rules_file(tmp_path)
    for args in (
        ("ta", "US1"), ("fa", "US1"), ("valuation", "US1"), ("flags", "US1"), ("xray",),
        ("risk", "--candidate", "US2", "--weight", "5"), ("screen", "--rules", rules), ("score",),
    ):  # fmt: skip
        assert call(env, *args).exit_code == 0, args
    after = snapshot(env[1])
    assert after.keys() == before.keys()
    for table in before:
        assert after[table] == before[table], table


def test_no_network_is_attempted(env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("the analysis commands must not open a network connection")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    rules = rules_file(tmp_path)
    for args in (("ta", "US1"), ("xray",), ("screen", "--rules", rules), ("score",)):
        assert call(env, *args).exit_code == 0, args


def test_help_lists_the_new_commands(cli_env: Env) -> None:
    r = runner.invoke(app, [*cli_env[0], "--help"])
    assert r.exit_code == 0
    for name in ("ta", "fa", "valuation", "flags", "xray", "risk", "screen", "score"):
        assert f" {name} " in r.output, name
