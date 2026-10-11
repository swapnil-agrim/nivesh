"""A run that spends nothing: row, dated directory and trace, with no gate or egress check."""

import sqlite3
from pathlib import Path

import pytest

from nivesh_cli.common import plain_run
from tests.cli.test_metered_run import loaded
from tests.run_paths import run_col

Env = tuple[list[str], Path]


def rows(env: Env) -> list[tuple[str, str, float, str]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute(
            "SELECT command, status, cost_inr, run_dir FROM run ORDER BY id"
        ).fetchall()
    finally:
        c.close()


def test_plain_run_writes_run_row_dated_dir_and_closes_ok(cli_env: Env) -> None:
    import re

    settings, _ = loaded(cli_env)
    with plain_run(settings, "probe") as m:
        assert m.run_dir.is_dir() and m.run_id == 1 and m.env == {}
        m.status = "ok"
    (row,) = rows(cli_env)
    assert row[:3] == ("probe", "ok", 0.0) and re.fullmatch(run_col(1), row[3])


def test_plain_run_error_sets_error_status(cli_env: Env) -> None:
    settings, _ = loaded(cli_env)
    with pytest.raises(RuntimeError), plain_run(settings, "probe"):
        raise RuntimeError("boom")
    assert rows(cli_env)[0][1] == "error"
