import sqlite3
from pathlib import Path

import pytest

from nivesh_core.db import MIGRATIONS, migrate

SQLITE = MIGRATIONS / "sqlite"


def upto(tmp_path: Path, n: int) -> Path:
    d = tmp_path / f"v{n}"
    d.mkdir(exist_ok=True)
    for f in sorted(SQLITE.glob("*.sql")):
        if int(f.name[:4]) <= n:
            (d / f.name).write_text(f.read_text())
    return d


def v4_db(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, upto(tmp_path, 4))
    c.execute("INSERT INTO account (id, name, kind, created_at) VALUES (1, 'a', 'manual', 't')")
    c.execute(
        "INSERT INTO ingest (id, kind, source_label, as_of, created_at, report) "
        "VALUES (1, 'csv', 'l', '2026-01-05', 't', '{}')"
    )
    c.execute(
        "INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'AAA', 'NSE', 'INR')"
    )
    c.execute(
        "INSERT INTO holding_snapshot (id, ingest_id, account_id, security_id, holder_ref, "
        "quantity, avg_cost, price, price_basis, value_inr, as_of, source, plan) "
        "VALUES (7, 1, 1, 1, 'h', '10', '5', '6', 'ltp', '60', '2026-01-05', 'csv', 'p')"
    )
    return c


def test_sqlite_0005_upgrades_v4_db_keeping_snapshots_as_inr(tmp_path: Path) -> None:
    c = v4_db(tmp_path)
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 5
    row = c.execute(
        "SELECT id, holder_ref, quantity, avg_cost, price, value_inr, plan, currency "
        "FROM holding_snapshot"
    ).fetchone()
    assert row == (7, "h", "10", "5", "6", "60", "p", "INR")


def test_value_inr_is_nullable_after_0005(tmp_path: Path) -> None:
    c = v4_db(tmp_path)
    migrate.apply(c, SQLITE)
    c.execute(
        "INSERT INTO holding_snapshot (ingest_id, account_id, security_id, quantity, price, "
        "price_basis, value_inr, as_of, source, currency) "
        "VALUES (1, 1, 1, '1', '2', 'ltp', NULL, '2026-01-05', 'alpaca', 'USD')"
    )
    assert c.execute("SELECT value_inr FROM holding_snapshot WHERE id = 8").fetchone() == (None,)


def test_lot_table_requires_existing_security_ingest_account(tmp_path: Path) -> None:
    c = v4_db(tmp_path)
    migrate.apply(c, SQLITE)
    sql = (
        "INSERT INTO lot (ingest_id, account_id, security_id, acquired_on, quantity, "
        "cost_per_unit, currency) VALUES (?, ?, ?, '2026-01-01', '1', '2', 'USD')"
    )
    c.execute(sql, (1, 1, 1))
    for bad in ((9, 1, 1), (1, 9, 1), (1, 1, 9)):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, bad)


def test_0005_rerun_is_noop_and_failure_rolls_back(tmp_path: Path) -> None:
    c = v4_db(tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    for f in upto(tmp_path, 4).glob("*.sql"):
        (broken / f.name).write_text(f.read_text())
    good = (SQLITE / "0005_us_holdings.sql").read_text()
    (broken / "0005_us_holdings.sql").write_text(good + "\nINSERT INTO nope VALUES (1);\n")
    with pytest.raises(migrate.MigrationError):
        migrate.apply(c, broken)
    cols = [r[1] for r in c.execute("PRAGMA table_info(holding_snapshot)")]
    assert "currency" not in cols and migrate.current_version(c) == 4
    assert c.execute("SELECT value_inr FROM holding_snapshot").fetchone() == ("60",)
    migrate.apply(c, SQLITE)
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE)


def test_holding_snapshot_index_recreated(tmp_path: Path) -> None:
    c = v4_db(tmp_path)
    migrate.apply(c, SQLITE)
    names = {r[1] for r in c.execute("PRAGMA index_list(holding_snapshot)")}
    assert "holding_snapshot_ingest" in names
    assert "lot_ingest" in {r[1] for r in c.execute("PRAGMA index_list(lot)")}
