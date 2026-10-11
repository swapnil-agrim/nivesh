"""`nivesh deliver` and `--deliver`: wiring only, nothing is sent (mock HTTP, fake mail)."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.deliver as cdeliver
import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from tests.agents import committee_fx as fx
from tests.cli.test_ideas_cli import edit
from tests.cli.test_ideas_report_cli import clean_pm
from tests.delivery_fx import DUMMIES, DUMMY_HOOK, Calls, FakeSmtp, body_json
from tests.ideas_fx import ASOF, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    c = Calls()
    monkeypatch.setattr(cdeliver, "http_client", lambda timeout: c.client())
    monkeypatch.setattr(cdeliver, "smtp_factory", lambda: FakeSmtp().factory)
    monkeypatch.setenv("S_HOOK", DUMMY_HOOK)
    return c


def enable_slack(env: Env) -> None:
    edit(env, "nivesh.yaml", "channels: []", "channels: [slack]")
    edit(env, "nivesh.yaml", "# slack_hook: ref:SLACK_HOOK", "slack_hook: ref:S_HOOK")


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def run_rows(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT id, command, status FROM run ORDER BY id").fetchall()
    finally:
        c.close()


def test_deliver_run_loads_report_from_the_dated_run_dir_via_find_run_dir(
    env: Env, calls: Calls
) -> None:
    enable_slack(env)
    assert call(env, "brief", "india").exit_code == 0
    assert run_path(env[1], 1).parent.name != "runs"  # dated directory
    r = call(env, "deliver", "1")
    assert r.exit_code == 0, r.output
    assert "deliver: slack sent" in r.output
    text = body_json(calls.requests[0])["text"]
    assert "Market brief" in text and "runs/20" in text and "/1/report.html" in text
    assert run_rows(env)[-1][1:] == ("deliver", "ok")


def test_deliver_unknown_run_exits_1(env: Env, calls: Calls) -> None:
    enable_slack(env)
    r = call(env, "deliver", "99")
    assert r.exit_code == 1 and "no run 99" in r.output and calls.requests == []


def test_deliver_run_without_a_saved_report_exits_1(env: Env, calls: Calls) -> None:
    enable_slack(env)
    call(env, "brief", "india")
    (run_path(env[1], 1) / "report.json").unlink()
    r = call(env, "deliver", "1")
    assert r.exit_code == 1 and "no saved report" in r.output


def test_deliver_channel_option_limits_to_one_channel(env: Env, calls: Calls) -> None:
    enable_slack(env)
    edit(env, "nivesh.yaml", "channels: [slack]", "channels: [slack, email]")
    call(env, "brief", "india")
    r = call(env, "deliver", "1", "--channel", "slack")
    assert r.exit_code == 0 and "slack sent" in r.output and "email" not in r.output
    assert call(env, "deliver", "1", "--channel", "fax").exit_code == 2


def test_deliver_with_no_channels_configured_says_nothing_sent(env: Env, calls: Calls) -> None:
    call(env, "brief", "india")
    r = call(env, "deliver", "1")
    assert r.exit_code == 0 and "nothing sent" in r.output and calls.requests == []


def test_deliver_flag_on_brief_sends_after_save(env: Env, calls: Calls) -> None:
    enable_slack(env)
    r = call(env, "brief", "india", "--deliver")
    assert r.exit_code == 0, r.output
    assert len(calls.requests) == 1 and "deliver: slack sent" in r.output
    assert (run_path(env[1], 1) / "report.json").is_file()


def test_deliver_flag_on_research_sends_after_finalize(
    env: Env, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_slack(env)
    monkeypatch.setattr(runtime, "query", fx.happy(pm=clean_pm))
    r = call(env, "research", "AAA", "--deliver")
    assert r.exit_code == 0, r.output
    assert len(calls.requests) == 1
    assert "Not investment advice" in body_json(calls.requests[0])["text"]


def test_draft_report_is_delivered_with_its_banner(env: Env, calls: Calls) -> None:
    enable_slack(env)
    assert call(env, "brief", "india").exit_code == 0
    p = run_path(env[1], 1) / "report.json"
    d = json.loads(p.read_text())
    d["banner"], d["unmatched"] = "DRAFT - UNVERIFIED NUMBERS", ["x"]
    p.write_text(json.dumps(d))
    r = call(env, "deliver", "1")
    assert r.exit_code == 0
    assert "DRAFT - UNVERIFIED NUMBERS" in body_json(calls.requests[0])["text"]


def test_delivery_failure_exits_0_and_warns(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    enable_slack(env)
    bad = Calls([500, 500, 500])
    monkeypatch.setattr(cdeliver, "http_client", lambda timeout: bad.client())
    monkeypatch.setenv("S_HOOK", DUMMY_HOOK)
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = call(env, "brief", "india", "--deliver")
    assert r.exit_code == 0, r.output
    assert "slack failed (http 500)" in r.output and "warning: slack not delivered" in r.output
    assert run_rows(env)[0][1:] == ("brief", "ok")


def test_deliver_prints_per_channel_outcome_without_urls(env: Env, calls: Calls) -> None:
    enable_slack(env)
    call(env, "brief", "india")
    r = call(env, "deliver", "1")
    assert "http" not in r.output and not [d for d in DUMMIES if d in r.output]


def test_holdings_runs_are_not_delivered_unless_asked(env: Env, calls: Calls) -> None:
    enable_slack(env)
    call(env, "brief", "india")
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    c.execute("UPDATE run SET command = 'review-portfolio' WHERE id = 1")
    c.commit()
    c.close()
    r = call(env, "deliver", "1")
    assert r.exit_code == 1 and "lists holdings" in r.output and calls.requests == []
    assert call(env, "deliver", "1", "--include-holdings").exit_code == 0
    assert len(calls.requests) == 1
