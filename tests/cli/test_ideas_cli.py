"""`nivesh ideas` over a synthetic store with the SDK faked: no network, no model, no PII."""

import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.db.duck import open_duck
from nivesh_core.ledger import calls_for_run
from nivesh_core.pii_scan import scan_text
from nivesh_core.timeutil import to_iso, utcnow
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import FakeSDK
from tests.ideas_fx import ASOF, hold, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

runner = CliRunner()
Env = tuple[list[str], Path]
D = Decimal


def edit(env: Env, name: str, old: str, new: str) -> None:
    p = Path(env[0][1]) / name
    text = p.read_text()
    assert old in text, old
    p.write_text(text.replace(old, new, 1))


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    sdk: FakeSDK = fx.happy()
    monkeypatch.setattr(runtime, "query", sdk)
    return sdk


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def sec_ids(fake: FakeSDK) -> set[int]:
    return {json.loads(c.prompt).get("security_id", 0) for c in fake.seen if c.agent == "pm"}


def run_count(env: Env) -> int:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return int(c.execute("SELECT count(*) FROM run").fetchone()[0])
    finally:
        c.close()


def ledger(env: Env, run_id: int = 1) -> list[Any]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return calls_for_run(c, run_id)
    finally:
        c.close()


def numbered(out: str) -> list[str]:
    return [x for x in out.splitlines() if re.match(r"^\d+\. ", x)]


def test_ideas_defaults_to_both_markets_n5_and_default_preset(env: Env, fake: FakeSDK) -> None:
    r = call(env, "ideas")
    assert r.exit_code == 0, r.output
    assert "preset lt-quality-value" in r.output
    assert "IN: 6 matches, 6 shortlisted" in r.output and "US: 6 matches, 6 shortlisted" in r.output
    assert len(numbered(r.output)) == 5
    assert "12 deep committee runs (estimate)" in r.output


def test_ideas_india_us_both_arguments_select_the_markets(env: Env, fake: FakeSDK) -> None:
    india = call(env, "ideas", "india", "2")
    assert india.exit_code == 0, india.output
    assert "IN:" in india.output and "US:" not in india.output and len(numbered(india.output)) == 2
    assert all("(IN)" in x for x in numbered(india.output))
    us = call(env, "ideas", "US", "1")
    assert "US:" in us.output and "IN:" not in us.output
    assert all("(US)" in x for x in numbered(us.output))
    named = call(env, "ideas", "india", "3", "lt-quality-growth")
    assert named.exit_code == 0 and "preset lt-quality-growth" in named.output


def test_ideas_runs_committee_on_shortlist_only(env: Env, fake: FakeSDK) -> None:
    edit(env, "nivesh.yaml", "  shortlist_size: 8", "  shortlist_size: 3")
    r = call(env, "ideas", "india")
    assert r.exit_code == 0, r.output
    assert len(sec_ids(fake)) == 3 and fake.counts["pm"] == 3  # of 12 stored securities
    assert "IN: 6 matches, 3 shortlisted" in r.output
    assert len({fx._sid(c) for c in fake.calls("fundamental")}) == 3


def test_ideas_uses_deep_tier(env: Env, fake: FakeSDK) -> None:
    assert call(env, "ideas", "india", "2").exit_code == 0
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT command, status, tier FROM run").fetchall() == [
        ("ideas", "needs_review", "deep")  # the fake verdict's entry zone has no evidence (E10)
    ]
    c.close()
    snap = json.loads((run_path(env[1], 1) / "snapshot.json").read_text())
    assert snap["tier"] == "deep"
    assert fake.counts.get("lens_value", 0) > 0 and fake.counts["bull"] > 0  # deep-only stages


def test_ideas_cost_gate_refusal_exits_1_without_calling_committee(env: Env, fake: FakeSDK) -> None:
    edit(env, "profile.yaml", "monthly_cost_cap: 2000", "monthly_cost_cap: 1000")
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    c.execute(
        "insert into run (command, started_at, status, cost_inr) values ('x', ?, 'ok', 1000)",
        (to_iso(utcnow()),),
    )
    c.commit()
    c.close()
    r = call(env, "ideas", "india")
    assert r.exit_code == 1 and "deep runs refused" in r.output
    assert fake.seen == [] and run_count(env) == 1  # no model call, no new run row
    assert ledger(env, 1) == []


def test_report_lists_thesis_entry_zone_invalidation_weight_and_what_would_prove_it_wrong(
    env: Env, fake: FakeSDK
) -> None:
    r = call(env, "ideas", "india", "1")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    head = numbered(r.output)[0]
    assert "BUY  conviction medium" in head
    for want in (
        "   thesis: Quality compounder",
        "   entry zone: 100 to 110 INR",
        "   suggested weight: 3%",
        "   invalidation: Margins fall two quarters in a row",
        "   what would prove this wrong: Valuation risk",
        "   review by 2026-04-02; preset lt-quality-value",
    ):
        assert want in lines
    assert any("not advice" in x for x in lines)


def test_report_says_only_k_of_n_qualified(env: Env, fake: FakeSDK) -> None:
    r = call(env, "ideas", "india", "5")
    assert re.search(r"only \d of 5 ideas qualified; no padding", r.output)
    assert len(numbered(r.output)) < 5
    many = call(env, "ideas", "india", "1")
    assert "ideas qualified" not in many.output


def test_held_name_shown_as_add_candidate(env: Env, fake: FakeSDK) -> None:
    hold(env[1], 0, heavy=5)  # AAA, small; FFF, large (the position limit applies to AAA)
    r = call(env, "ideas", "india", "6")
    line = next(x for x in numbered(r.output) if " AAA " in x)
    assert line.endswith("[ADD candidate]")
    assert not any("ADD candidate" in x for x in numbered(r.output) if " AAA " not in x)


def test_every_committee_verdict_recorded_in_the_ledger_with_reported_flag_for_top_n(
    env: Env, fake: FakeSDK
) -> None:
    r = call(env, "ideas", "india", "2", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    rows = ledger(env)
    assert len(rows) == 6 == out["ledger_rows"]  # every committee verdict, not only the top n
    reported = {x.security_id for x in rows if x.reported}
    top = {i["verdict"]["security_id"] for i in out["ideas"]}
    assert len(top) == 2 and reported == top
    assert {x.verdict for x in rows} >= {"BUY", "INSUFFICIENT_DATA"}
    assert all(x.preset == "lt-quality-value" and x.run_id == 1 for x in rows)


def test_ledger_row_has_run_id_input_hash_prompt_and_model_versions_and_last_close(
    env: Env, fake: FakeSDK
) -> None:
    assert call(env, "ideas", "india", "2").exit_code == 0
    rows = ledger(env)
    snap = json.loads((run_path(env[1], 1) / "snapshot.json").read_text())
    duck = open_duck(env[1] / "nivesh.duckdb", read_only=True)
    try:
        for row in rows:
            want = hashlib.sha256(
                json.dumps(
                    {"as_of": snap["as_of"],
                     "engine_outputs": snap["engine_outputs"][str(row.security_id)]},
                    sort_keys=True,
                ).encode()
            ).hexdigest()  # fmt: skip
            assert row.input_hash == want and len(row.input_hash) == 64
            assert row.prompt_versions == snap["prompt_versions"] and row.prompt_versions["pm"]
            assert row.model_versions == snap["models"]
            last = duck.execute(
                "SELECT close FROM price_bar WHERE security_id = ? ORDER BY date DESC LIMIT 1",
                (row.security_id,),
            ).fetchone()
            assert row.last_close == D(str(last[0]))
            bench = (
                duck.execute(
                    "SELECT close FROM price_bar b JOIN (SELECT 1) ON 1=1 WHERE security_id = "
                    "(SELECT 0) LIMIT 1"
                ).fetchone()
                if False
                else None
            )
            assert bench is None and row.benchmark_level is not None  # BENCHIN configured
    finally:
        duck.close()
    assert len({r.input_hash for r in rows}) == len(rows)


def test_ledger_benchmark_level_is_the_configured_index_close(env: Env, fake: FakeSDK) -> None:
    assert call(env, "ideas", "india", "1").exit_code == 0
    duck = open_duck(env[1] / "nivesh.duckdb", read_only=True)
    sql = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        bid = sql.execute("SELECT id FROM security WHERE symbol = 'BENCHIN'").fetchone()[0]
        last = duck.execute(
            "SELECT close FROM price_bar WHERE security_id = ? ORDER BY date DESC LIMIT 1", (bid,)
        ).fetchone()[0]
    finally:
        duck.close()
        sql.close()
    assert {r.benchmark_level for r in ledger(env)} == {D(str(last))}


def test_ledger_benchmark_missing_is_null_with_a_reason(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch, fake: FakeSDK
) -> None:
    seed_ideas_store(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)  # no benchmarks configured
    # rs-free preset: only fundamentals
    assert call(cli_env, "ideas", "india", "1").exit_code == 0
    rows = ledger(cli_env)
    assert rows and all(r.benchmark_level is None for r in rows)
    assert all("no benchmark configured for market IN" in (r.benchmark_reason or "") for r in rows)


def test_a_failed_security_is_reported_as_skipped_and_the_run_continues(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = fx.happy()
    ccc = None
    sql = sqlite3.connect(env[1] / "nivesh.sqlite")
    ccc = sql.execute("SELECT id FROM security WHERE symbol = 'CCC'").fetchone()[0]
    sql.close()
    good = base.by_agent["pm"]

    def pm(call_: Any) -> Any:
        if json.loads(call_.prompt)["security_id"] == ccc:
            return RuntimeError("model unavailable")
        return good(call_)

    sdk = fx.happy(pm=pm)
    monkeypatch.setattr(runtime, "query", sdk)
    r = call(env, "ideas", "india", "6")
    assert r.exit_code == 0, r.output
    assert "skipped CCC: no verdict (insufficient data)" in r.output
    assert any(" AAA " in x for x in numbered(r.output))
    assert {x.security_id: x.verdict for x in ledger(env)}[ccc] == "INSUFFICIENT_DATA"


def test_empty_universe_exits_1_with_load_hint_and_runs_no_agent(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch, fake: FakeSDK
) -> None:
    seed_ideas_store(cli_env[1], load=False)
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    r = call(cli_env, "ideas", "india")
    assert r.exit_code == 1
    assert "no members loaded for NIFTY500; run `nivesh universe load`" in r.output
    assert fake.seen == [] and run_count(cli_env) == 0


def test_stores_closed_before_agent_calls(env: Env, fake: FakeSDK) -> None:
    opened: list[bool] = []

    def hook(_call: Any) -> None:
        try:  # a writable DuckDB handle fails while this process still holds a read-only one
            duckdb.connect(str(env[1] / "nivesh.duckdb")).close()
            opened.append(True)
        except duckdb.Error:
            opened.append(False)

    fake.on_start = hook
    assert call(env, "ideas", "india", "1").exit_code == 0
    assert opened and all(opened)


def test_ideas_json_is_exact_decimals_and_sorted_keys(env: Env, fake: FakeSDK) -> None:
    r = call(env, "ideas", "india", "2", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert r.stdout.strip() == json.dumps(data, sort_keys=True)
    first = data["ideas"][0]
    v = first["verdict"]
    assert first["rank"] == 1 and v["verdict"] == "BUY"
    assert isinstance(v["suggested_weight_pct"], str) and D(v["suggested_weight_pct"]) == D(3)
    assert v["entry_zone"] == {"ccy": "INR", "high": "110", "low": "100"}
    assert data["command"] == "ideas" and data["run_id"] == 1


def test_ideas_spends_nothing_when_no_candidate_matches(env: Env, fake: FakeSDK) -> None:
    r = call(env, "ideas", "india", "5", "pos-breakout")  # no setup breaks out in this store
    assert r.exit_code == 0, r.output
    assert "nothing was run and nothing was spent" in r.output
    assert fake.seen == [] and run_count(env) == 0
    j = call(env, "ideas", "india", "5", "pos-breakout", "--json")
    assert json.loads(j.stdout)["ideas"] == [] and run_count(env) == 0


def test_prompt_to_agents_carries_identifiers_no_quantities_or_holder_refs(
    env: Env, fake: FakeSDK
) -> None:
    hold(env[1], 0, 1)
    assert call(env, "ideas", "india", "6").exit_code == 0
    assert fake.seen
    for c in fake.seen:
        low = c.prompt.lower()
        for word in ("quantity", "avg_cost", "holder", "value_inr", "price_basis", "unrealised"):
            assert word not in low, (c.agent, word)
        assert scan_text(c.prompt) == [], c.agent


def test_unknown_preset_or_market_exits_2(env: Env, fake: FakeSDK) -> None:
    for args in (("ideas", "mars"), ("ideas", "india", "5", "nope"), ("ideas", "india", "0")):
        r = call(env, *args)
        assert r.exit_code == 2, (args, r.output)
    assert fake.seen == [] and run_count(env) == 0
    assert "unknown preset 'nope'" in call(env, "ideas", "india", "5", "nope").output


def test_total_runs_are_capped_and_the_estimate_is_printed_before_spending(
    env: Env, fake: FakeSDK
) -> None:
    edit(env, "nivesh.yaml", "  max_runs: 16", "  max_runs: 4")
    r = call(env, "ideas", "both")
    assert r.exit_code == 1 and "12 committee runs, above ideas.max_runs (4)" in r.output
    assert fake.seen == [] and run_count(env) == 0
    edit(env, "nivesh.yaml", "  max_runs: 4", "  max_runs: 12")
    ok = call(env, "ideas", "both", "1")
    assert ok.exit_code == 0 and "12 deep committee runs (estimate)" in ok.output
    assert ok.output.index("12 deep committee runs") < ok.output.index("ideas as of")


def test_stale_membership_prints_a_warning_and_still_runs(
    env: Env, fake: FakeSDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit(env, "nivesh.yaml", "  max_bar_age_days: 7", "  max_bar_age_days: 90")
    monkeypatch.setattr(cengine, "_today", lambda: ASOF.replace(month=3))
    r = call(env, "ideas", "india", "1")
    assert r.exit_code == 0, r.output
    assert "warning: NIFTY500 membership is" in r.output


def test_report_step_failure_keeps_the_ledger_rows_and_flags_needs_review(
    env: Env, fake: FakeSDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nivesh_cli.ideas as cideas

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("detail that must stay out")

    monkeypatch.setattr(cideas, "save_report", boom)
    r = call(env, "ideas", "india", "2")
    assert r.exit_code == 0, r.output
    assert "report step failed (RuntimeError)" in r.output and "must stay out" not in r.output
    assert len(ledger(env)) > 0
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    assert c.execute("SELECT status FROM run").fetchall() == [("needs_review",)]
    c.close()
