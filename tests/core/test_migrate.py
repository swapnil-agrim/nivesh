import shutil
import sqlite3
from pathlib import Path

import pytest

from nivesh_core.db import migrate
from nivesh_core.db.migrate import MigrationError

ROOT = Path(__file__).resolve().parents[2]
SQLITE_DIR = ROOT / "nivesh_core" / "db" / "migrations" / "sqlite"
FIXTURE = ROOT / "tests" / "fixtures" / "db" / "v1_sqlite.sql"


LATEST = migrate.latest_version(SQLITE_DIR)
NEXT = LATEST + 1


def conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def migrations_with_next(tmp_path: Path, sql: str) -> Path:
    d = tmp_path / "m"
    shutil.copytree(SQLITE_DIR, d)
    (d / f"{NEXT:04d}_add_col.sql").write_text(sql)
    return d


def test_fresh_db_gets_latest_version() -> None:
    c = conn()
    migrate.apply(c, SQLITE_DIR)
    assert migrate.current_version(c) == LATEST
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    assert {"schema_version", "security", "account", "run"} <= tables


def test_upgrade_preserves_fixture_rows(tmp_path: Path) -> None:
    c = conn()
    c.executescript(FIXTURE.read_text())
    d = migrations_with_next(tmp_path, "ALTER TABLE security ADD COLUMN sector TEXT;")
    migrate.apply(c, d)
    assert migrate.current_version(c) == NEXT
    rows = c.execute("select symbol, name, sector from security order by id").fetchall()
    assert rows == [("TESTCO", "Test Co", None), ("DEMO", "Demo Inc", None)]
    migrate.apply(c, d)  # rerun is a no-op
    assert migrate.current_version(c) == NEXT
    assert c.execute("select count(*) from schema_version").fetchone() == (NEXT,)


def test_failing_migration_rolls_back(tmp_path: Path) -> None:
    c = conn()
    c.executescript(FIXTURE.read_text())
    bad = "ALTER TABLE security ADD COLUMN sector TEXT;\nTHIS IS NOT SQL;"
    d = migrations_with_next(tmp_path, bad)
    with pytest.raises(MigrationError, match=f"{NEXT:04d}_add_col"):
        migrate.apply(c, d)
    assert migrate.current_version(c) == LATEST
    cols = [r[1] for r in c.execute("pragma table_info(security)")]
    assert "sector" not in cols  # first statement was rolled back too
    assert not c.in_transaction


def test_downgrade_refused() -> None:
    c = conn()
    migrate.apply(c, SQLITE_DIR)
    c.execute("insert into schema_version values (99, 'future', 'x')")
    with pytest.raises(MigrationError, match="newer"):
        migrate.apply(c, SQLITE_DIR)


def test_semicolon_in_string_literal_is_one_statement(tmp_path: Path) -> None:
    c = conn()
    migrate.apply(c, SQLITE_DIR)
    d = migrations_with_next(
        tmp_path, "CREATE TABLE note (t TEXT);\nINSERT INTO note VALUES ('a;b');"
    )
    migrate.apply(c, d)
    assert c.execute("select t from note").fetchone() == ("a;b",)


def test_0002_on_v1_fixture_keeps_rows_and_defaults() -> None:
    c = conn()
    c.executescript(FIXTURE.read_text())
    c.execute("insert into run (command, started_at, status) values ('old', 't', 'ok')")
    migrate.apply(c, SQLITE_DIR)
    assert migrate.current_version(c) == LATEST
    assert c.execute("select count(*) from security").fetchone()[0] >= 1
    row = c.execute(
        "select command, run_dir, model, input_tokens, output_tokens, paid_data_inr, tier from run"
    ).fetchone()
    assert row == ("old", None, None, 0, 0, 0.0, "quick")
    migrate.apply(c, SQLITE_DIR)  # rerun is a no-op
    assert c.execute("select count(*) from schema_version").fetchone() == (LATEST,)
    idx = {r[1] for r in c.execute("pragma index_list(run)")}
    assert "run_started" in idx


def v2_db() -> sqlite3.Connection:
    """A fully migrated in-memory database with foreign keys on."""
    c = conn()
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE_DIR)
    return c


def test_0003_upgrades_v2_database_and_keeps_existing_rows(tmp_path: Path) -> None:
    c = conn()
    c.executescript(FIXTURE.read_text())
    c.execute("insert into run (command, started_at, status) values ('old', 't', 'ok')")
    migrate.apply(c, SQLITE_DIR)
    assert migrate.current_version(c) >= 3
    assert c.execute("select count(*) from run").fetchone() == (1,)
    row = c.execute(
        "select symbol, market, unresolved, amfi_code from security order by id"
    ).fetchall()
    assert row == [("TESTCO", "IN", 0, None), ("DEMO", "IN", 0, None)]


def test_0003_creates_holdings_tables() -> None:
    c = v2_db()
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    assert {"ingest", "ingest_holder", "holding_snapshot", "txn", "meta"} <= tables


def test_unresolved_isin_placeholder_fits_security_unique_constraint() -> None:
    c = v2_db()
    ins = (
        "insert into security (symbol, exchange, currency, isin, unresolved) "
        "values ('INE000A01010', 'ISIN', 'INR', 'INE000A01010', 1)"
    )
    c.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins)


def test_account_kind_name_is_unique() -> None:
    c = v2_db()
    ins = "insert into account (name, kind, created_at) values ('CAS demat', 'cas_demat', 't')"
    c.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins)


def seed_refs(c: sqlite3.Connection) -> None:
    c.execute("insert into account (name, kind, created_at) values ('a', 'k', 't')")
    c.execute("insert into security (symbol, exchange, currency) values ('S', 'NSE', 'INR')")
    c.execute(
        "insert into ingest (kind, source_label, as_of, created_at, report) "
        "values ('k', 'l', 'd', 't', '{}')"
    )


def test_snapshot_requires_existing_account_and_security() -> None:
    c = v2_db()
    seed_refs(c)
    sql = (
        "insert into holding_snapshot (ingest_id, account_id, security_id, quantity, price, "
        "price_basis, value_inr, as_of, source) values (1, ?, ?, '1', '1', 'nav', '1', 'd', 's')"
    )
    c.execute(sql, (1, 1))
    for acc, sec in ((99, 1), (1, 99)):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, (acc, sec))


def test_txn_unique_index_ignores_overlapping_statement_rows() -> None:
    c = v2_db()
    seed_refs(c)
    sql = (
        "insert or ignore into txn (ingest_id, account_id, security_id, holder_ref, date, type, "
        "quantity, amount, occurrence) values (1, 1, 1, 'r', '2026-01-01', 'buy', '1', '5', ?)"
    )
    c.execute(sql, (0,))
    c.execute(sql, (0,))
    assert c.execute("select count(*) from txn").fetchone() == (1,)
    c.execute(sql, (1,))  # the second identical row inside one statement is kept
    assert c.execute("select count(*) from txn").fetchone() == (2,)


def test_0003_rerun_is_noop() -> None:
    c = v2_db()
    before = c.execute("select count(*) from schema_version").fetchone()
    migrate.apply(c, SQLITE_DIR)
    assert c.execute("select count(*) from schema_version").fetchone() == before
