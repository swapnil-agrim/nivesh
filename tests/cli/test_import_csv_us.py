import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import latest_holdings, latest_lots
from nivesh_core.pii_scan import scan_text

runner = CliRunner()
Env = tuple[list[str], Path]
FX = Path(__file__).resolve().parents[1] / "fixtures" / "csv"
HEADER = "account_label,symbol,exchange,quantity,avg_cost,currency,asset_class,acquired_on\n"
GOOD = HEADER + "US Broker,AAPL,NASDAQ,10,150.25,USD,equity,2025-03-14\n"


def write(tmp: Path, text: str, name: str = "us.csv") -> Path:
    p = tmp / name
    p.write_text(text)
    return p


def seed_msft(data: Path) -> None:
    init_stores(data)
    c = open_sqlite(data / "nivesh.sqlite")
    c.execute(
        "INSERT INTO security (symbol, exchange, name, currency, market) "
        "VALUES ('MSFT', 'NASDAQ', 'Microsoft Corp', 'USD', 'US')"
    )
    c.close()


def test_import_csv_market_us_stores_holdings_and_lots(cli_env: Env, tmp_path: Path) -> None:
    args, data = cli_env
    seed_msft(data)
    r = runner.invoke(app, [*args, "import-csv", str(FX / "us_valid.csv"), "--market", "us"])
    assert r.exit_code == 0, r.output
    assert "imported 4 holdings and 3 lots into 1 account(s)" in r.output
    for secret in ("AAPL", "150.25", "MSFT", "US Broker"):
        assert secret not in r.output
    c = open_sqlite(data / "nivesh.sqlite")
    got = latest_holdings(c)
    assert {h.currency for h in got} == {"USD"} and {h.source for h in got} == {"us_csv"}
    assert len(latest_lots(c)) == 3
    assert c.execute("select kind from account").fetchone() == ("manual_us",)
    c.close()


def test_market_us_all_or_nothing_on_any_error(cli_env: Env) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(FX / "us_invalid.csv"), "--market", "us"])
    assert r.exit_code == 1 and "line 3:" in r.output and "line 13:" in r.output
    c = sqlite3.connect(data / "nivesh.sqlite")
    assert c.execute("select count(*) from ingest").fetchone() == (0,)
    assert c.execute("select count(*) from lot").fetchone() == (0,)


def test_same_file_twice_is_skipped_by_digest(cli_env: Env, tmp_path: Path) -> None:
    args, data = cli_env
    p = write(tmp_path, GOOD)
    runner.invoke(app, [*args, "import-csv", str(p), "--market", "us"])
    r = runner.invoke(app, [*args, "import-csv", str(p), "--market", "us"])
    assert r.exit_code == 0 and "already imported" in r.output
    c = sqlite3.connect(data / "nivesh.sqlite")
    assert c.execute("select count(*) from ingest").fetchone() == (1,)


def test_edited_file_replaces_lots(cli_env: Env, tmp_path: Path) -> None:
    args, data = cli_env
    runner.invoke(app, [*args, "import-csv", str(write(tmp_path, GOOD)), "--market", "us"])
    edited = write(tmp_path, GOOD.replace(",10,", ",12,"), "edited.csv")
    r = runner.invoke(app, [*args, "import-csv", str(edited), "--market", "us"])
    assert r.exit_code == 0, r.output
    c = open_sqlite(data / "nivesh.sqlite")
    assert [x.quantity for x in latest_lots(c)] == [12]
    assert [h.quantity for h in latest_holdings(c)] == [12]
    c.close()


def test_default_market_in_unchanged(cli_env: Env) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(FX / "valid.csv")])
    assert r.exit_code == 0 and "5 holdings" in r.output
    r = runner.invoke(app, [*args, "import-csv", str(FX / "us_valid.csv"), "--market", "in"])
    assert r.exit_code == 1 and "line 2:" in r.output


def test_preset_for_wrong_market_rejected_naming_valid_ones(cli_env: Env) -> None:
    args, _ = cli_env
    cmd = [*args, "import-csv", str(FX / "us_alpaca.csv"), "--label", "x", "--preset", "zerodha"]
    r = runner.invoke(app, [*cmd, "--market", "us"])
    assert r.exit_code == 1 and "alpaca, robinhood" in r.output
    r = runner.invoke(app, [*args, "import-csv", str(FX / "us_alpaca.csv"), "--label", "x",
                            "--preset", "alpaca"])  # fmt: skip
    assert r.exit_code == 1 and "zerodha" in r.output


def test_unknown_market_rejected(cli_env: Env) -> None:
    args, _ = cli_env
    r = runner.invoke(app, [*args, "import-csv", str(FX / "us_valid.csv"), "--market", "eu"])
    assert r.exit_code == 1 and "in or us" in r.output


def test_label_required_with_preset(cli_env: Env) -> None:
    args, _ = cli_env
    cmd = [*args, "import-csv", str(FX / "us_alpaca.csv"), "--market", "us", "--preset", "alpaca"]
    r = runner.invoke(app, cmd)
    assert r.exit_code == 1 and "--label" in r.output


def test_us_preset_imports(cli_env: Env) -> None:
    args, data = cli_env
    cmd = [*args, "import-csv", str(FX / "us_alpaca.csv"), "--market", "us", "--preset", "alpaca"]
    r = runner.invoke(app, [*cmd, "--label", "Paper"])
    assert r.exit_code == 0, r.output
    c = open_sqlite(data / "nivesh.sqlite")
    assert {h.source_label for h in latest_holdings(c)} == {"Paper"}
    c.close()


def test_us_import_output_and_logs_contain_no_pii_strings(cli_env: Env) -> None:
    args, _ = cli_env
    bad = runner.invoke(app, [*args, "import-csv", str(FX / "us_invalid.csv"), "--market", "us"])
    ok = runner.invoke(app, [*args, "import-csv", str(FX / "us_alpaca.csv"), "--market", "us",
                             "--preset", "alpaca", "--label", "P"])  # fmt: skip
    for r in (bad, ok):
        assert scan_text(r.output) == []
