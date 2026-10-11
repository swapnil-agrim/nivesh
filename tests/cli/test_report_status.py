"""`needs_review` as a run status: set by the citation check, nothing else changes."""

import sqlite3
from pathlib import Path

import pytest

from nivesh_adapters.report import Block, Table
from nivesh_adapters.report_check import finalize
from nivesh_cli.common import metered_run
from nivesh_core.cost import month_to_date
from nivesh_core.timeutil import utcnow
from tests.cli.test_metered_run import loaded
from tests.report_fx import base_report

Env = tuple[list[str], Path]


def rows(env: Env) -> list[tuple[str, float]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT status, cost_inr FROM run ORDER BY id").fetchall()
    finally:
        c.close()


def report(text: str):  # type: ignore[no-untyped-def]
    return base_report(
        summary=(),
        decision=Table(("a",), ()),
        blocks=(Block("b", "Body", text),),
        nums=(),
    )


def test_run_row_status_needs_review_after_a_draft_run(cli_env: Env) -> None:
    settings, profile = loaded(cli_env)
    with metered_run(settings, profile, "probe", "quick", force=False) as m:
        m.tracer.finish_run("claude-test")
        m.status = "ok"
        out = finalize(report("invented 123.45"), {}, m.run_dir, metered=m)
    assert out.banner and rows(cli_env)[0][0] == "needs_review"


def test_clean_ok_and_error_statuses_are_unchanged(cli_env: Env) -> None:
    settings, profile = loaded(cli_env)
    with metered_run(settings, profile, "probe", "quick", force=False) as m:
        m.status = "ok"
        finalize(report("no numbers here"), {}, m.run_dir, metered=m)
    with pytest.raises(RuntimeError), metered_run(settings, profile, "probe", "quick", force=False):
        raise RuntimeError("boom")
    assert [r[0] for r in rows(cli_env)] == ["ok", "error"]


def test_needs_review_does_not_change_month_to_date_cost(cli_env: Env) -> None:
    settings, profile = loaded(cli_env)
    with metered_run(settings, profile, "probe", "quick", force=False) as m:
        m.tracer.result(model="claude-test", input_tokens=1, output_tokens=1, cost_inr=12.5,
                        cost_source="test")  # fmt: skip
        m.status = "ok"
        finalize(report("invented 123.45"), {}, m.run_dir, metered=m)
    c = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    try:
        assert month_to_date(c, utcnow()) == 12.5
    finally:
        c.close()
    assert rows(cli_env) == [("needs_review", 12.5)]
