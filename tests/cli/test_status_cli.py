"""`nivesh status` keeps its cost lines and adds health lines; nothing is written."""

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nivesh_adapters.investright_session import TokenStore, token_path
from nivesh_cli.main import app
from tests.agents.test_runtime import cfg_dir, seed_spend

runner = CliRunner()


def test_status_keeps_the_four_pinned_cost_substrings_and_adds_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = cfg_dir(tmp_path, cap=1000)
    seed_spend(tmp_path, 850)
    r = runner.invoke(app, [*cfg, "status"])
    assert r.exit_code == 0, r.output
    for s in ("850.00", "1000.00", "85%", "downgraded"):
        assert s in r.output
    assert "hdfc session: absent" in r.output and "last ingest: none yet" in r.output
    assert "cache: empty" in r.output and "last runs:" in r.output and "run 1 x ok" in r.output


def test_status_prints_no_session_value_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = cfg_dir(tmp_path)
    seed_spend(tmp_path, 1)
    TokenStore(token_path(tmp_path / "dd")).save("session-value-for-test")
    before = (
        sqlite3.connect(tmp_path / "dd" / "nivesh.sqlite")
        .execute("SELECT count(*) FROM run")
        .fetchone()
    )
    r = runner.invoke(app, [*cfg, "status"])
    assert "session-value-for-test" not in r.output and "hdfc session: valid" in r.output
    after = (
        sqlite3.connect(tmp_path / "dd" / "nivesh.sqlite")
        .execute("SELECT count(*) FROM run")
        .fetchone()
    )
    assert before == after
