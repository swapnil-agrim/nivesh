"""`nivesh thesis list | show` over a synthetic store (no PII, no network, no model)."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_core.thesis_store import save_thesis
from tests.cli.test_engine_cli import snapshot
from tests.core.test_thesis import thesis
from tests.holdings_fx import holding
from tests.run_paths import run_col, run_path

D = Decimal
runner = CliRunner()
Env = tuple[list[str], Path]
ASOF = date(2026, 10, 1)


def seed(data: Path) -> dict[str, int]:
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        rows = [
            holding(isin="INE000A01010", symbol="RELI", value_inr=D(5000)),
            holding(isin="INE000A01010", symbol="RELI", holder_ref="h2", value_inr=D(1000)),
            holding(isin="INE111A01011", symbol="ETFX", asset_class="etf", value_inr=D(2000)),
            holding(isin="INE222B01012", symbol="TCSX", value_inr=D(3000)),
            holding(isin="INE333C01013", symbol="FUNDY", asset_class="mf", exchange="AMFI"),
        ]
        save_ingest(
            sql, kind="investright", source_label="broker", digest=None, as_of=ASOF,
            holdings=rows, txns=[], holder_refs=["", "h2"], warnings=[],
        )  # fmt: skip
        ids = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
        save_thesis(sql, thesis(security_id=ids["RELI"], review_date=date(2026, 9, 1)))
        save_thesis(
            sql,
            thesis(
                security_id=ids["ETFX"], horizon="positional_1_6m", review_date=date(2027, 1, 1)
            ),
        )
        return ids
    finally:
        sql.close()


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def test_list_shows_symbol_horizon_review_date_and_marks_overdue_reviews(env: Env) -> None:
    r = call(env, "thesis", "list")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    reli = next(x for x in lines if x.startswith("RELI"))
    etfx = next(x for x in lines if x.startswith("ETFX"))
    assert "long_term_1y_plus" in reli and "2026-09-01" in reli and "overdue" in reli
    assert "positional_1_6m" in etfx and "2027-01-01" in etfx and "overdue" not in etfx


def test_list_names_equity_holdings_without_a_thesis_and_says_funds_are_out_of_scope(
    env: Env,
) -> None:
    r = call(env, "thesis", "list")
    assert "without a thesis: TCSX" in r.output
    assert "FUNDY" not in r.output
    assert "mutual funds are out of scope" in r.output


def test_show_prints_why_and_each_criterion_with_metric_and_threshold(env: Env) -> None:
    r = call(env, "thesis", "show", "RELI")
    assert r.exit_code == 0, r.output
    assert "Durable franchise" in r.output and "review date: 2026-09-01" in r.output
    assert "1. criterion 1 breaks [roce_pct lt 12.5 %]" in r.output
    assert "2. criterion 2 breaks [judged by the reviewer]" in r.output


def test_show_unknown_symbol_exits_1_with_message(env: Env) -> None:
    r = call(env, "thesis", "show", "NOPE")
    assert r.exit_code == 1 and "no security matches" in r.output
    r = call(env, "thesis", "show", "TCSX")
    assert r.exit_code == 1 and "no active thesis for TCSX" in r.output


def test_list_and_show_open_the_store_read_only(env: Env) -> None:
    before = snapshot(env[1])
    for args in (("thesis", "list"), ("thesis", "show", "RELI"), ("thesis", "show", "TCSX")):
        call(env, *args)
    assert snapshot(env[1]) == before


# ---- thesis onboard (fake SDK: no network, no model) -----------------------------------------
import json  # noqa: E402
import sqlite3  # noqa: E402

from nivesh_agents import runtime  # noqa: E402
from nivesh_core.thesis_store import active_theses as _active  # noqa: E402
from nivesh_core.trace import read_trace  # noqa: E402
from tests.agents import committee_fx as fx  # noqa: E402
from tests.agents.fake_sdk import Call, reply  # noqa: E402


def _sid(call: Call) -> int:
    body = json.loads(call.prompt)
    return int(body["security_id"] if "security_id" in body else body["security"]["security_id"])


@pytest.fixture
def onboard(env: Env, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The env store with the fake SDK; dates pinned to the fixtures' as_of."""
    monkeypatch.setattr(cengine, "_today", lambda: fx.AS_OF)
    fake = fx.happy(thesis_draft=lambda c: reply(fx.thesis_draft(security_id=_sid(c))))
    monkeypatch.setattr(runtime, "query", fake)
    return fake


def theses(env: Env) -> dict[int, Any]:
    sql = open_sqlite(env[1] / "nivesh.sqlite")
    try:
        return _active(sql)
    finally:
        sql.close()


def ids(env: Env) -> dict[str, int]:
    sql = open_sqlite(env[1] / "nivesh.sqlite")
    try:
        return {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
    finally:
        sql.close()


def close_reli(env: Env) -> None:
    sql = open_sqlite(env[1] / "nivesh.sqlite")
    try:
        sql.execute(
            "UPDATE thesis SET status = 'closed' WHERE security_id = ?", (ids(env)["RELI"],)
        )
    finally:
        sql.close()


def run_rows(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute(
            "SELECT command, status, tier, cost_inr, prompt_version, run_dir FROM run"
        ).fetchall()
    finally:
        c.close()


def test_onboard_accept_stores_thesis_with_created_and_review_dates(env: Env, onboard: Any) -> None:
    r = runner.invoke(app, [*env[0], "thesis", "onboard"], input="a\n")
    assert r.exit_code == 0, r.output
    assert "TCSX (long_term_1y_plus)" in r.output and "Durable franchise" in r.output
    t = theses(env)[ids(env)["TCSX"]]
    assert (t.created_at, t.review_date) == (date(2026, 1, 2), date(2026, 4, 2))
    assert t.source == "onboarding" and "stored thesis for TCSX" in r.output


def test_onboard_skip_stores_nothing(env: Env, onboard: Any) -> None:
    r = runner.invoke(app, [*env[0], "thesis", "onboard"], input="s\n")
    assert r.exit_code == 0, r.output
    assert ids(env)["TCSX"] not in theses(env) and len(theses(env)) == 2
    assert "skipped TCSX" in r.output


def test_onboard_edit_prompts_each_field_validates_and_stores(env: Env, onboard: Any) -> None:
    answers = ["e", "New reason to own.", "positional_1_6m", "", "", "", "15", "", "", "", ""]
    r = runner.invoke(app, [*env[0], "thesis", "onboard"], input="\n".join(answers) + "\n")
    assert r.exit_code == 0, r.output
    t = theses(env)[ids(env)["TCSX"]]
    assert t.why == "New reason to own." and t.horizon == "positional_1_6m"
    assert t.kill_criteria[0].threshold == Decimal(15) and t.kill_criteria[1].metric is None


def test_onboard_invalid_edit_reasks_then_skip_stores_nothing(env: Env, onboard: Any) -> None:
    answers = ["e", "word " * 61, "", "", "", "", "", "", "", "", "", "s"]
    r = runner.invoke(app, [*env[0], "thesis", "onboard"], input="\n".join(answers) + "\n")
    assert r.exit_code == 0, r.output
    assert "60 words" in r.output and r.output.count("accept, edit or skip") == 2
    assert ids(env)["TCSX"] not in theses(env)


def test_onboard_only_symbol_limits_to_one_holding(env: Env, onboard: Any) -> None:
    close_reli(env)
    r = runner.invoke(app, [*env[0], "thesis", "onboard", "--only", "TCSX"], input="a\n")
    assert r.exit_code == 0, r.output
    assert {_sid(c) for c in onboard.calls("thesis_draft")} == {ids(env)["TCSX"]}
    assert ids(env)["RELI"] not in theses(env)


def test_onboard_with_every_holding_covered_says_so_and_spends_nothing(
    env: Env, onboard: Any
) -> None:
    runner.invoke(app, [*env[0], "thesis", "onboard"], input="a\n")
    before = run_rows(env)
    r = runner.invoke(app, [*env[0], "thesis", "onboard"])
    assert r.exit_code == 0 and "every equity and ETF holding has an active thesis" in r.output
    assert run_rows(env) == before and len(onboard.seen) == 4


def test_onboard_failed_draft_is_reported_and_the_loop_continues(
    env: Env, monkeypatch: pytest.MonkeyPatch, onboard: Any
) -> None:
    close_reli(env)
    reli = ids(env)["RELI"]

    def draft(call: Call) -> Any:
        sid = _sid(call)
        return RuntimeError("down") if sid == reli else reply(fx.thesis_draft(security_id=sid))

    monkeypatch.setattr(runtime, "query", fx.happy(thesis_draft=draft))
    r = runner.invoke(app, [*env[0], "thesis", "onboard"], input="a\n")
    assert r.exit_code == 0, r.output
    assert "RELI: skipped (draft failed" in r.output
    assert ids(env)["TCSX"] in theses(env) and reli not in theses(env)


def test_onboard_run_row_and_trace_record_cost_and_prompt_versions(env: Env, onboard: Any) -> None:
    runner.invoke(app, [*env[0], "thesis", "onboard"], input="s\n")
    ((command, status, tier, cost, ver, rdir),) = run_rows(env)
    assert (command, status, tier) == ("thesis onboard", "ok", "quick")
    assert re.fullmatch(run_col(1), rdir)
    assert cost > 0 and ver == "thesis_draft:v1"
    recs = read_trace(run_path(env[1], 1) / "trace.jsonl")
    starts = {x["agent"]: x["prompt_version"] for x in recs if x["type"] == "agent_start"}
    assert starts == {"fundamental": "v1", "technical": "v1", "news": "v1", "thesis_draft": "v1"}
    assert recs[-1]["type"] == "result" and recs[-1]["cost_inr"] == cost


def test_held_securities_prefers_the_resolved_row_when_a_placeholder_shares_the_isin(
    tmp_path: Path,
) -> None:
    from nivesh_cli.thesis import held_securities
    from tests.adapters.test_review_service import placeholder_then_resolved

    rid = placeholder_then_resolved(tmp_path / "data")
    sql = open_sqlite(tmp_path / "data" / "nivesh.sqlite")
    try:
        assert [h.security_id for h in held_securities(sql)] == [rid]
    finally:
        sql.close()


def test_onboard_edit_dash_clears_a_metric_and_one_machine_criterion_stays_required(
    env: Env, onboard: Any
) -> None:
    clear_only = ["e", "", "", "", "-", "-", "-", "", "", "", ""]
    swap = ["e", "", "", "", "-", "-", "-", "", "pe", "gt", "30"]
    r = runner.invoke(
        app, [*env[0], "thesis", "onboard"], input="\n".join([*clear_only, *swap]) + "\n"
    )
    assert r.exit_code == 0, r.output
    assert "machine-checkable" in r.output and r.output.count("accept, edit or skip") == 2
    c1, c2 = theses(env)[ids(env)["TCSX"]].kill_criteria
    assert (c1.metric, c1.comparator, c1.threshold) == (None, None, None)
    assert (c2.metric, c2.comparator, c2.threshold) == ("pe", "gt", Decimal(30))
