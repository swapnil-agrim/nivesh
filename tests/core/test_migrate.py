import shutil
import sqlite3
from pathlib import Path

import pytest

from nivesh_core.db import migrate
from nivesh_core.db.migrate import MigrationError

ROOT = Path(__file__).resolve().parents[2]
SQLITE_DIR = ROOT / "nivesh_core" / "db" / "migrations" / "sqlite"
FIXTURE = ROOT / "tests" / "fixtures" / "db" / "v1_sqlite.sql"


def conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def migrations_with_0002(tmp_path: Path, sql: str) -> Path:
    d = tmp_path / "m"
    shutil.copytree(SQLITE_DIR, d)
    (d / "0002_add_col.sql").write_text(sql)
    return d


def test_fresh_db_gets_latest_version() -> None:
    c = conn()
    migrate.apply(c, SQLITE_DIR)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE_DIR) == 1
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    assert {"schema_version", "security", "account", "run"} <= tables


def test_upgrade_preserves_fixture_rows(tmp_path: Path) -> None:
    c = conn()
    c.executescript(FIXTURE.read_text())
    d = migrations_with_0002(tmp_path, "ALTER TABLE security ADD COLUMN sector TEXT;")
    migrate.apply(c, d)
    assert migrate.current_version(c) == 2
    rows = c.execute("select symbol, name, sector from security order by id").fetchall()
    assert rows == [("TESTCO", "Test Co", None), ("DEMO", "Demo Inc", None)]
    migrate.apply(c, d)  # rerun is a no-op
    assert migrate.current_version(c) == 2
    assert c.execute("select count(*) from schema_version").fetchone() == (2,)


def test_failing_migration_rolls_back(tmp_path: Path) -> None:
    c = conn()
    c.executescript(FIXTURE.read_text())
    bad = "ALTER TABLE security ADD COLUMN sector TEXT;\nTHIS IS NOT SQL;"
    d = migrations_with_0002(tmp_path, bad)
    with pytest.raises(MigrationError, match="0002_add_col"):
        migrate.apply(c, d)
    assert migrate.current_version(c) == 1
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
    d = migrations_with_0002(
        tmp_path, "CREATE TABLE note (t TEXT);\nINSERT INTO note VALUES ('a;b');"
    )
    migrate.apply(c, d)
    assert c.execute("select t from note").fetchone() == ("a;b",)
