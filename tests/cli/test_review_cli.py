"""`nivesh review ...` over a synthetic store (no PII, no network, no model)."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from tests.adapters.test_review_service import ASOF, seed
from tests.cli.test_engine_cli import snapshot
from tests.holdings_fx import ISIN_B

runner = CliRunner()
Env = tuple[list[str], Path]
MF_TAX = "tax: {long_term_days: 365, short_rate_pct: 20, long_rate_pct: 12.5}"


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed(cli_env[1])
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    text = cfg.read_text()
    old = "  tax: {}  "
    assert old in text
    cfg.write_text(text.replace(old, f"  {MF_TAX}  "))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def test_review_tax_prints_per_lot_rows_and_the_not_advice_label(env: Env) -> None:
    r = call(env, "review", "tax", "AAPL")
    assert r.exit_code == 0, r.output
    assert "estimate from your config; not tax advice" in r.output
    assert "2025-01-02  qty 4  days 637  long-term yes  days to long-term 0  gain 240.00" in (
        r.output
    )
    assert "2026-06-01  qty 2  days 122  long-term no  days to long-term 243  gain 20.00" in (
        r.output
    )
    assert "tax now n/a" in r.output and "note: US tax not modelled" in r.output
    f = call(env, "review", "tax", ISIN_B)
    assert f.exit_code == 0, f.output
    assert "tax now 25.00  at long-term 25.00  saving 0.00" in f.output  # 200 x 12.5%
    assert "total: gain 250.00  tax now 35.00  at long-term 31.25  saving 3.75" in f.output


def test_review_tax_trim_prints_cheapest_lots_and_fifo_note(env: Env) -> None:
    r = call(env, "review", "tax", ISIN_B, "--trim", "5")
    assert r.exit_code == 0, r.output
    assert "trim 5: 2025-01-06 x 5; gain 100.00; tax 12.50" in r.output
    assert "FIFO assumed for this holding (oldest units first)" in r.output
    assert "estimated tax 12.50 vs 10.00 for the lowest-tax lots, difference 2.50" in r.output
    over = call(env, "review", "tax", ISIN_B, "--trim", "99")
    assert over.exit_code == 0 and "quantity 99 exceeds the open quantity 15" in over.output


def test_review_tax_json_uses_exact_decimal_strings(env: Env) -> None:
    r = call(env, "review", "tax", "AAPL", "--trim", "1", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout, parse_float=lambda s: pytest.fail(f"float {s}"))
    assert data["command"] == "review tax" and data["subject"] == "AAPL"
    assert data["label"] == "estimate from your config; not tax advice"
    assert data["report"]["lots"][0]["gain"] == "240.00"
    assert data["reason"] == "US tax not modelled" and data["currency"] == "USD"
    assert data["trim"]["picks"][0][1] == "1"
    assert Decimal(data["report"]["total_gain"]) == Decimal("260.00")


def test_review_tax_for_indian_equity_prints_unavailable_and_exits_0(env: Env) -> None:
    r = call(env, "review", "tax", "RELI")
    assert r.exit_code == 0, r.output
    assert "holding period unavailable: no dated lots" in r.output
    assert "not tax advice" in r.output


def test_review_tax_for_a_security_not_held_exits_1(env: Env) -> None:
    r = call(env, "review", "tax", "NOPE")
    assert r.exit_code == 1 and "no security matches" in r.output


def test_review_tax_never_writes_to_the_stores(env: Env) -> None:
    before = snapshot(env[1])
    for args in (("AAPL",), (ISIN_B, "--trim", "5"), ("RELI", "--json")):
        assert call(env, "review", "tax", *args).exit_code == 0
    assert snapshot(env[1]) == before


# ---- review holdings (fake SDK: no network, no model) ----------------------------------------
import sqlite3  # noqa: E402
from datetime import date, timedelta  # noqa: E402

from nivesh_agents import runtime  # noqa: E402
from nivesh_agents.schemas import HoldingReview  # noqa: E402
from nivesh_core.db.sqlite import open_sqlite  # noqa: E402
from nivesh_core.errors import NiveshError  # noqa: E402
from nivesh_core.thesis_store import save_thesis  # noqa: E402
from tests.agents import committee_fx as fx  # noqa: E402
from tests.agents.fake_sdk import Call, reply, script  # noqa: E402
from tests.cli.test_engine_cli import ASOF as CLI_ASOF  # noqa: E402
from tests.cli.test_engine_cli import seed_cli_store  # noqa: E402
from tests.core.test_thesis import thesis  # noqa: E402


def reviewer(call: Call) -> Any:
    body = json.loads(call.prompt)
    payload = fx.review(
        security_id=body["security_id"], as_of=body["as_of"],
        criteria=[fx.crit_check(criterion_id=1, status="not_met"),
                  fx.crit_check(criterion_id=2, status="not_met", evidence=[fx.ev(2)])],
    )  # fmt: skip
    return reply(payload, tools=fx.calls("mcp__engine__fa_compute", 1, 2))


@pytest.fixture
def held(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Any:
    """RELI (with a thesis) and US1 (without one) over stored bars; the reviewer is faked."""
    seed_cli_store(cli_env[1])
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    try:
        ids = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
        created = CLI_ASOF - timedelta(days=10)
        save_thesis(sql, thesis(security_id=ids["RELI"], created_at=created,
                                review_date=created + timedelta(days=90)))  # fmt: skip
    finally:
        sql.close()
    monkeypatch.setattr(cengine, "_today", lambda: CLI_ASOF)
    fake = script(holding_review=reviewer)
    monkeypatch.setattr(runtime, "query", fake)
    return fake, ids


def run_rows(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, status, tier, prompt_version FROM run").fetchall()
    finally:
        c.close()


def test_review_holdings_table_has_action_confidence_reasons_triggers_and_tax_note(
    cli_env: Env, held: Any
) -> None:
    r = runner.invoke(app, [*cli_env[0], "review", "holdings"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    head = next(x for x in lines if x.startswith("RELI"))
    assert head.split()[1:3] == ["TRIM", "medium"]  # concentration over the 10% profile limit
    assert "  reason thesis_intact: Returns hold up" in lines
    assert any(x.startswith("  concentration: triggered") for x in lines)
    assert any(x.startswith("  kill_criterion: not_evaluable") for x in lines)
    assert "  tax: estimate from your config; not tax advice: holding period unavailable" in (
        r.output
    )
    assert "action lowered from HOLD to TRIM: concentration" in r.output
    assert "criterion 1: no code value to confirm not_met; kept as unknown" in r.output
    assert run_rows(cli_env) == [("review holdings", "ok", "quick", "holding_review:v1")]


def test_review_holdings_lists_holdings_without_thesis_and_spends_nothing_for_them(
    cli_env: Env, held: Any
) -> None:
    fake, ids = held
    r = runner.invoke(app, [*cli_env[0], "review", "holdings"])
    assert "without a thesis: US1 (run `nivesh thesis onboard`)" in r.output
    assert [json.loads(c.prompt)["security_id"] for c in fake.seen] == [ids["RELI"]]
    only = runner.invoke(app, [*cli_env[0], "review", "holdings", "--only", "US1"])
    assert only.exit_code == 0 and "nothing to review" in only.output
    assert len(fake.seen) == 1 and len(run_rows(cli_env)) == 1  # no call, no run row


def test_review_holdings_json_is_schema_valid(cli_env: Env, held: Any) -> None:
    r = runner.invoke(app, [*cli_env[0], "review", "holdings", "--only", "RELI", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["command"] == "review holdings" and data["as_of"] == CLI_ASOF.isoformat()
    assert data["without_thesis"] == []
    (one,) = data["reviews"]
    review = HoldingReview.model_validate_json(json.dumps(one))
    assert review.action == "TRIM" and [t.code for t in review.triggers][0] == "kill_criterion"
    assert isinstance(date.fromisoformat(one["as_of"]), date)


# ---- review rebalance (no model, no write) ---------------------------------------------------
def test_review_rebalance_prints_moves_pro_forma_turnover_and_per_proposal_cap_note(
    env: Env,
) -> None:
    r = call(env, "review", "rebalance")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("review rebalance as of 2026-10-01 (band +-5pp, turnover cap 30%")
    assert any(x.split()[:3] == ["reduce", "equity", "Reliance"] for x in lines)
    assert any(x.split()[:3] == ["add", "debt", "-"] and "(proceeds)" in x for x in lines)
    assert "  equity       66.67 -> 65.00 vs 60.0" in lines
    assert "turnover: 1.67% of the portfolio" in lines
    assert "note: per-proposal cap; annual turnover not tracked yet" in lines
    assert "note: no target for unclassified: left as is" in lines
    assert "note: still out of band: debt, gold" in lines
    assert "note: Apple Inc: left out (no INR value)" in lines
    assert "note: no review run given: no holding is flagged EXIT or TRIM" in lines


def test_review_rebalance_with_cash_option(env: Env) -> None:
    r = call(env, "review", "rebalance", "--cash", "1000")
    assert r.exit_code == 0, r.output
    cash = [x.split() for x in r.output.splitlines() if "(new cash)" in x]
    assert [(x[1], x[3]) for x in cash] == [("debt", "525.00"), ("equity", "300.00"),
                                           ("gold", "175.00")]  # fmt: skip
    for bad in ("-5", "abc", "NaN"):
        b = call(env, "review", "rebalance", "--cash", bad)
        assert b.exit_code == 1 and "--cash must be" in b.output


def test_review_rebalance_json_exact_strings(env: Env) -> None:
    r = call(env, "review", "rebalance", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout, parse_float=lambda s: pytest.fail(f"float {s}"))
    plan = data["plan"]
    assert data["command"] == "review rebalance" and plan["turnover_pct"] == "1.67"
    assert {m["kind"] for m in plan["moves"]} == {"add", "reduce"}
    assert sum(Decimal(x["after_inr"]) for x in plan["pro_forma"]) == Decimal(1800)


def test_review_rebalance_spends_nothing_and_writes_nothing(env: Env) -> None:
    call(env, "review", "tax", "RELI")  # stores exist before the snapshot
    before = snapshot(env[1])
    for args in ((), ("--cash", "500"), ("--json",)):
        assert call(env, "review", "rebalance", *args).exit_code == 0
    assert snapshot(env[1]) == before
    assert not (env[1] / "runs").exists()


def write_review(data: Path, run_id: int, n: int, payload: dict[str, Any]) -> None:
    out = data / "runs" / str(run_id) / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{n:03d}_holding_review_{payload.get('security_id', 0)}.json").write_text(
        json.dumps(payload)
    )


def test_review_rebalance_uses_review_run_flags_and_skips_invalid_files(env: Env) -> None:
    sql = sqlite3.connect(env[1] / "nivesh.sqlite")
    reli = sql.execute("SELECT id FROM security WHERE symbol = 'RELI'").fetchone()[0]
    sql.close()
    write_review(env[1], 7, 1, fx.review(security_id=reli, action="EXIT"))
    write_review(env[1], 7, 2, {"status": "failed", "reason": "reviewer_unavailable"})
    r = call(env, "review", "rebalance", "--review-run", "7")
    assert r.exit_code == 0, r.output
    assert "Reliance                 30.00  (review EXIT)" in r.output
    assert "note: 002_holding_review_0.json: skipped (not a valid holding review)" in r.output


def test_review_run_id_must_be_an_integer_path_component(env: Env) -> None:
    for bad in ("../7", "7/outputs", "x"):
        assert call(env, "review", "rebalance", "--review-run", bad).exit_code == 2
    for missing in ("0", "-1", "99"):
        r = call(env, "review", "rebalance", "--review-run", missing)
        assert r.exit_code == 1, (missing, r.output)
    from nivesh_cli.review import review_flags

    with pytest.raises(NiveshError):
        review_flags(env[1], True)  # a bool is not a run number


def test_review_tax_trim_not_finite_exits_1_with_message(env: Env) -> None:
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = call(env, "review", "tax", ISIN_B, "--trim", bad)
        assert r.exit_code == 1 and isinstance(r.exception, SystemExit), bad
        assert "--trim must be a finite number" in r.output, bad
