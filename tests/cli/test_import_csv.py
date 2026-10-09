import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import latest_holdings

runner = CliRunner()
Env = tuple[list[str], Path]
FX = Path(__file__).resolve().parents[1] / "fixtures" / "csv"


def test_import_saves_snapshots_per_account_label_and_prints_count(cli_env: Env) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(FX / "valid.csv")])
    assert r.exit_code == 0, r.output
    assert "5 holdings" in r.output and "3 account" in r.output
    c = open_sqlite(data / "nivesh.sqlite")
    got = latest_holdings(c)
    assert {h.source_label for h in got} == {"Other Broker", "Retirement", "Bank deposits"}
    assert c.execute("select kind from account where name = 'Retirement'").fetchone() == ("manual",)
    c.close()


def test_import_with_errors_prints_line_numbers_exits_1_and_saves_nothing(cli_env: Env) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(FX / "invalid.csv")])
    assert r.exit_code == 1 and "line 3:" in r.output and "line 8:" in r.output
    c = sqlite3.connect(data / "nivesh.sqlite")
    assert c.execute("select count(*) from ingest").fetchone() == (0,)


def test_import_with_preset_requires_label(cli_env: Env) -> None:
    args, _ = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(FX / "zerodha.csv"), "--preset", "zerodha"])
    assert r.exit_code == 1 and "--label" in r.output


def test_import_with_preset_converts_and_saves(cli_env: Env) -> None:
    args, data = cli_env
    cmd = [*args, "import-csv", str(FX / "groww.csv"), "--preset", "groww", "--label", "My Groww"]
    r = runner.invoke(app, cmd)
    assert r.exit_code == 0, r.output
    c = open_sqlite(data / "nivesh.sqlite")
    assert {h.source_label for h in latest_holdings(c)} == {"My Groww"}


def test_unknown_preset_exits_1(cli_env: Env) -> None:
    args, _ = cli_env
    cmd = [*args, "import-csv", str(FX / "groww.csv"), "--preset", "nope", "--label", "x"]
    r = runner.invoke(app, cmd)
    assert r.exit_code == 1 and "zerodha" in r.output


def test_preset_missing_column_exits_1(cli_env: Env) -> None:
    args, _ = cli_env
    cmd = [*args, "import-csv", str(FX / "groww.csv"), "--preset", "zerodha", "--label", "x"]
    r = runner.invoke(app, cmd)
    assert r.exit_code == 1 and "line 1" in r.output


def test_import_same_file_twice_is_skipped_by_digest(cli_env: Env) -> None:
    args, data = cli_env
    runner.invoke(app, [*args, "import-csv", str(FX / "valid.csv")])
    r = runner.invoke(app, [*args, "import-csv", str(FX / "valid.csv")])
    assert r.exit_code == 0 and "already imported" in r.output
    c = sqlite3.connect(data / "nivesh.sqlite")
    assert c.execute("select count(*) from ingest").fetchone() == (1,)


def test_import_missing_file_exits_1(cli_env: Env, tmp_path: Path) -> None:
    args, _ = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(tmp_path / "nope.csv")])
    assert r.exit_code == 1 and "not found" in r.output
