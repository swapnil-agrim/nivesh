"""No personal data in anything thesis onboarding or the holding review writes (E8)."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.pii_scan import scan_paths, scan_text
from tests import pii_values as pv
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import Call, reply
from tests.cli.test_thesis_cli import Env, seed

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()
HOSTILE = f"contact {pv.email()} or call {pv.phone()} about {pv.pan()}"


def _sid(call: Call) -> int:
    body = json.loads(call.prompt)
    return int(body["security_id"] if "security_id" in body else body["security"]["security_id"])


def test_onboard_and_review_runs_over_synthetic_holdings_leave_no_pii_in_run_dir_trace_or_thesis_rows(  # noqa: E501
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: fx.AS_OF)

    def news(call: Call) -> Any:
        v = fx.analyst("news", security_id=_sid(call), key_points=[fx.point(1)])
        return reply(v, tools=fx.calls("mcp__news__get_news", 1, content=HOSTILE))

    def reviewer(call: Call) -> Any:
        v = fx.review(security_id=_sid(call), as_of=fx.AS_OF.isoformat())
        return reply(v, tools=fx.calls("mcp__engine__fa_compute", 1, content=HOSTILE))

    fake = fx.happy(
        news=news,
        thesis_draft=lambda c: reply(fx.thesis_draft(security_id=_sid(c))),
        holding_review=reviewer,
    )
    monkeypatch.setattr(runtime, "query", fake)
    r = runner.invoke(app, [*cli_env[0], "thesis", "onboard"], input="a\n")
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, [*cli_env[0], "review", "holdings", "--json"])
    assert r.exit_code == 0, r.output
    runs = cli_env[1] / "runs"
    files = [p for p in runs.rglob("*") if p.is_file()]
    assert {p.parent.parent.name for p in files if p.parent.name == "outputs"} == {"1", "2"}
    assert scan_paths([runs]) == []
    for trace in runs.rglob("trace.jsonl"):
        text = trace.read_text()
        assert pv.email() not in text and pv.pan() not in text and pv.phone() not in text
    sql = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    try:
        rows = sql.execute("SELECT why, kill_criteria FROM thesis").fetchall()
    finally:
        sql.close()
    assert len(rows) == 3 and all(scan_text(" ".join(row)) == [] for row in rows)
    assert scan_text(r.stdout) == []


def test_new_fixtures_prompts_and_schemas_have_no_key_shaped_literals() -> None:
    files = [
        *(ROOT / "prompts" / "thesis_draft").glob("v*.md"),
        *(ROOT / "prompts" / "holding_review").glob("v*.md"),
        ROOT / "schemas" / "ThesisDraft.json", ROOT / "schemas" / "HoldingReview.json",
        ROOT / "config" / "nivesh.yaml", ROOT / "config" / "profile.yaml",
        *(ROOT / "tests" / p for p in (
            "agents/committee_fx.py", "agents/test_thesis_agent.py",
            "agents/test_holding_review.py", "adapters/test_review_service.py",
            "cli/test_review_cli.py", "cli/test_thesis_cli.py", "cli/test_metered_run.py",
            "core/test_thesis.py", "core/test_thesis_store.py", "core/test_review_config.py",
            "engine/test_tax_lots.py", "engine/test_review_rules.py", "engine/test_rebalance.py",
        )),
    ]  # fmt: skip
    assert len(files) == 19
    for f in files:
        assert scan_text(f.read_text()) == [], f
