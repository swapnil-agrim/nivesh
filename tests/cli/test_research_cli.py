"""`nivesh research`: the full committee on one security, saved as a checked note."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
import nivesh_cli.research as cresearch
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_paths, scan_text
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import FakeSDK, reply
from tests.cli.test_ideas_cli import edit
from tests.cli.test_ideas_report_cli import clean_pm
from tests.ideas_fx import ASOF, hold, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def install(monkeypatch: pytest.MonkeyPatch, **replace: Any) -> FakeSDK:
    replace.setdefault("pm", clean_pm)
    sdk: FakeSDK = fx.happy(**replace)
    monkeypatch.setattr(runtime, "query", sdk)
    return sdk


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def runs(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, status, tier, cost_inr FROM run").fetchall()
    finally:
        c.close()


def test_research_defaults_to_deep_tier(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    r = call(env, "research", "AAA")
    assert r.exit_code == 0, r.output
    assert runs(env)[0][:3] == ("research", "ok", "deep")


def test_research_quick_flag(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    assert call(env, "research", "AAA", "quick").exit_code == 0
    assert runs(env)[0][2] == "quick"
    assert call(env, "research", "AAA", "huge").exit_code == 2


def test_research_runs_full_committee_and_saves_note_in_dated_run_dir(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = install(monkeypatch)
    r = call(env, "research", "AAA")
    assert r.exit_code == 0, r.output
    assert {"fundamental", "technical", "news", "pm"} <= {c.agent for c in sdk.seen}
    d = run_path(env[1], 1)
    for name in ("report.md", "report.html", "report_input.json", "report.json", "snapshot.json"):
        assert (d / name).is_file(), name
    md = (d / "report.md").read_text()
    assert md.startswith("# Research note: Example AAA") and "Quality compounder" in md
    assert "DRAFT" not in md and r.stdout.startswith("# Research note")
    assert scan_paths([d]) == []


def test_research_prefix_and_ambiguity(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    seed = sqlite3.connect(env[1] / "nivesh.sqlite")
    seed.execute(
        "INSERT INTO security (symbol, exchange, currency, market, name) "
        "VALUES ('AAA', 'NASDAQ', 'USD', 'US', 'Other AAA')"
    )
    seed.commit()
    seed.close()
    bad = call(env, "research", "AAA")
    assert bad.exit_code == 1 and "ambiguous" in bad.output and "NSE: or US:" in bad.output
    assert runs(env) == []
    assert call(env, "research", "NSE:AAA").exit_code == 0


def test_cost_gate_refusal_exits_1_without_calling_committee(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = install(monkeypatch)
    edit(env, "profile.yaml", "monthly_cost_cap: 2000", "monthly_cost_cap: 100")
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    c.execute(
        "INSERT INTO run (command, started_at, status, cost_inr) "
        "VALUES ('x', '2026-01-02T00:00:00+00:00', 'ok', 1000)"
    )
    c.commit()
    c.close()
    monkeypatch.setattr(
        "nivesh_cli.common.utcnow",
        lambda: __import__("datetime").datetime(2026, 1, 2, tzinfo=__import__("datetime").UTC),
    )
    r = call(env, "research", "AAA")
    assert r.exit_code == 1 and "refused" in r.output
    assert sdk.seen == [] and len(runs(env)) == 1


def test_stores_closed_before_agent_calls(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = install(monkeypatch)
    opened: list[bool] = []

    def hook(_call: Any) -> None:
        try:
            duckdb.connect(str(env[1] / "nivesh.duckdb")).close()
            opened.append(True)
        except duckdb.Error:
            opened.append(False)

    sdk.on_start = hook
    assert call(env, "research", "AAA").exit_code == 0
    assert opened and all(opened)


def test_prompt_to_agents_carries_identifiers_no_quantities_or_holder_refs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = install(monkeypatch)
    hold(env[1], 0, 1)
    assert call(env, "research", "AAA").exit_code == 0
    assert sdk.seen
    for c in sdk.seen:
        low = c.prompt.lower()
        for word in ("quantity", "avg_cost", "holder", "value_inr", "price_basis", "unrealised"):
            assert word not in low, (c.agent, word)
        assert scan_text(c.prompt) == [], c.agent


def test_uses_real_usd_inr_not_the_float_default(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real = cresearch.run_committee

    async def spy(*a: Any, **k: Any) -> Any:
        seen.append(k["usd_inr"])
        return await real(*a, **k)

    monkeypatch.setattr(cresearch, "run_committee", spy)
    install(monkeypatch)
    edit(env, "nivesh.yaml", "usd_inr: 90.0", "usd_inr: 83.5")
    assert call(env, "research", "AAA").exit_code == 0
    assert seen == [83.5]


def test_invented_number_in_bull_case_yields_draft_and_needs_review(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def pm(c: Any) -> Any:
        return reply(
            fx.verdict(security_id=fx._sid(c), entry_zone=None, bull_case="Sales reach 4321.5")
        )

    install(monkeypatch, pm=pm)
    r = call(env, "research", "AAA")
    assert r.exit_code == 0, r.output
    assert runs(env)[0][1] == "needs_review"
    md = (run_path(env[1], 1) / "report.md").read_text()
    assert md.startswith("> **DRAFT - UNVERIFIED NUMBERS**") and "4321.5" in md
    assert "warning: DRAFT" in r.output and r.stdout.startswith("> **DRAFT")


def test_failed_committee_prints_error_and_saves_no_report(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)

    async def broken(*a: Any, **k: Any) -> Any:
        raise NiveshError("committee unavailable")

    monkeypatch.setattr(cresearch, "run_committee", broken)
    r = call(env, "research", "AAA")
    assert r.exit_code == 1 and "committee unavailable" in r.output
    assert runs(env)[0][1] == "error"
    assert not list((env[1] / "runs").rglob("report*"))


def test_json_output_exact_decimals_sorted_keys(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    r = call(env, "research", "AAA", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert r.stdout.strip() == json.dumps(data, sort_keys=True, ensure_ascii=False)
    assert data["title"] == "Research note: Example AAA"
