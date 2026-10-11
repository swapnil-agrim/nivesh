"""`nivesh ideas` saves a checked report beside its unchanged stdout (ST-10.2, ST-10.6)."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
import nivesh_cli.ideas as cideas
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.cost import month_to_date
from nivesh_core.pii_scan import scan_paths
from nivesh_core.timeutil import utcnow
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import FakeSDK, reply
from tests.ideas_fx import ASOF, seed_ideas_store, with_benchmarks
from tests.run_paths import run_col, run_path

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def install(monkeypatch: pytest.MonkeyPatch, **replace: Any) -> FakeSDK:
    sdk: FakeSDK = fx.happy(**replace)
    monkeypatch.setattr(runtime, "query", sdk)
    return sdk


def clean_pm(call: Any) -> Any:
    """A verdict with no entry zone: every number in the report is code-produced."""
    return reply(
        fx.verdict(security_id=fx._sid(call), verdict="BUY", suggested_weight_pct=3,
                   entry_zone=None, bull_case="Quality compounder", bear_case="Valuation risk")
    )  # fmt: skip


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def row(env: Env) -> tuple[str, str, str]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT status, run_dir, cost_inr FROM run").fetchone()  # type: ignore[no-any-return]
    finally:
        c.close()


def test_ideas_saves_report_files_in_the_dated_run_dir(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, pm=clean_pm)
    r = call(env, "ideas", "india", "2")
    assert r.exit_code == 0, r.output
    d = run_path(env[1], 1)
    assert re.fullmatch(run_col(1), row(env)[1])
    for name in ("report.md", "report.html", "report_input.json", "report.json", "trace.jsonl"):
        assert (d / name).is_file(), name
    md = (d / "report.md").read_text()
    assert md.startswith("# Ideas") and "Quality compounder" in md and "DRAFT" not in md
    assert row(env)[0] == "ok" and "DRAFT" not in r.output
    assert scan_paths([d]) == []


def test_ideas_stdout_is_unchanged(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    r = call(env, "ideas", "india", "2")
    assert r.stdout.startswith("ideas as of 2026-01-02 (deep committee, preset lt-quality-value;")
    assert "report" not in r.stdout.lower().replace("reported", "") and "DRAFT" not in r.stdout
    assert "recorded 6 calls in the ledger (run 1)" in r.stdout


def test_ideas_json_output_unchanged(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    r = call(env, "ideas", "india", "2", "--json")
    assert r.exit_code == 0
    assert sorted(json.loads(r.stdout)) == [
        "as_of", "command", "ideas", "ledger_rows", "message", "preset", "qualified",
        "run_id", "shortlists", "skipped", "warnings",
    ]  # fmt: skip


def test_no_candidate_run_saves_no_report(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    r = call(env, "ideas", "india", "5", "pos-breakout")
    assert r.exit_code == 0 and not (env[1] / "runs").exists()


def test_ideas_invented_entry_zone_yields_draft_and_needs_review(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)  # the fake verdict proposes a zone no tool result supports
    r = call(env, "ideas", "india", "2")
    assert r.exit_code == 0, r.output
    status, _, cost = row(env)
    assert status == "needs_review"
    d = run_path(env[1], 1)
    md = (d / "report.md").read_text()
    assert md.startswith("> **DRAFT - UNVERIFIED NUMBERS**") and "100 in" in md
    assert (d / "report.html").read_text().index("DRAFT - UNVERIFIED NUMBERS") < 600
    assert "warning: DRAFT - UNVERIFIED NUMBERS" in r.output
    assert "DRAFT" not in r.stdout  # the banner goes to stderr, not into the result
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        assert month_to_date(c, utcnow()) == float(cost)  # the status does not change cost
    finally:
        c.close()


def test_model_number_absent_from_report_input_json(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    call(env, "ideas", "india", "2")
    facts = (run_path(env[1], 1) / "report_input.json").read_text()
    assert "Quality compounder" not in facts and "BUY" not in facts and '"110"' not in facts
    assert "last_close" in facts and "by_security" in facts


def test_a_report_that_cannot_be_saved_marks_the_run_needs_review(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, pm=clean_pm)

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cideas, "save_report", boom)
    r = call(env, "ideas", "india", "2")
    assert r.exit_code == 0 and row(env)[0] == "needs_review"  # the ledger rows are kept
