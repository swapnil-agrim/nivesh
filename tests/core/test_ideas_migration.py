import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nivesh_core import backup
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from tests.core.test_backup import RECIPIENT
from tests.core.test_thesis_store import v5_db
from tests.core.test_us_holdings_migration import upto

SQLITE = MIGRATIONS / "sqlite"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
TRIGGERS = {"ledger_entry_no_update", "ledger_entry_no_delete"}


def test_migration_0007_on_a_0006_store_keeps_holdings_theses_and_adds_tables(
    tmp_path: Path,
) -> None:
    c = v5_db(tmp_path)
    migrate.apply(c, upto(tmp_path, 6))
    c.execute(
        "INSERT INTO thesis (security_id, created_at, horizon, why, kill_criteria, "
        "target_review_date, status, source) VALUES (1, 't', 'positional_1_6m', 'w', '[]', "
        "'2026-04-01', 'active', 'onboarding')"
    )
    assert migrate.current_version(c) == 6
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 7
    assert c.execute("SELECT id, value_inr FROM holding_snapshot").fetchall() == [(7, "60")]
    assert c.execute("SELECT count(*) FROM thesis").fetchone() == (1,)
    for t in ("index_member", "ledger_entry", "watch"):
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone() == (0,)  # noqa: S608


def test_fresh_store_is_at_version_7(tmp_path: Path) -> None:
    init_stores(tmp_path / "d")
    c = sqlite3.connect(tmp_path / "d" / "nivesh.sqlite")
    assert c.execute("SELECT max(version) FROM schema_version").fetchone()[0] >= 7
    assert sorted(p.name for p in SQLITE.glob("*.sql"))[-1] == "0007_ideas.sql"


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'A', 'NSE', 'INR')")
    c.execute("INSERT INTO run (id, command, started_at, status) VALUES (1, 'ideas', 't', 'ok')")
    return c


def test_index_member_unique_per_index_and_security() -> None:
    c = fresh()
    c.execute("INSERT INTO index_member VALUES ('N', 1, '2026-10-01')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO index_member VALUES ('N', 1, '2026-10-02')")
    c.execute("INSERT INTO index_member VALUES ('M', 1, '2026-10-01')")  # another index is fine
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO index_member VALUES ('M', 99, '2026-10-01')")  # FK


def test_ledger_and_watch_tables_exist_with_check_constraints() -> None:
    c = fresh()
    cols = {r[1] for r in c.execute("PRAGMA table_info(ledger_entry)")}
    assert {"reported", "preset", "corrects_id", "created_at"} <= cols
    base = (
        "INSERT INTO ledger_entry (run_id, security_id, verdict, horizon, conviction, "
        "invalidation, review_date, input_hash, prompt_versions, model_versions, reported, "
        "created_at) VALUES (1, 1, ?, 'positional_1_6m', 'low', '[]', 'd', 'h', '{}', '{}', ?, 't')"
    )
    c.execute(base, ("BUY", 1))
    for verdict, reported in (("MAYBE", 1), ("BUY", 2)):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(base, (verdict, reported))
    with pytest.raises(sqlite3.IntegrityError):  # a zone is both ends or neither
        c.execute("INSERT INTO watch VALUES (1, '1', NULL, '2026-10-01')")
    c.execute("INSERT INTO watch VALUES (1, NULL, NULL, '2026-10-01')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO watch VALUES (1, NULL, NULL, '2026-10-01')")  # one row per security


def test_a_run_row_cannot_be_removed_while_ledger_rows_reference_it() -> None:
    c = fresh()
    c.execute(
        "INSERT INTO ledger_entry (run_id, security_id, verdict, horizon, conviction, "
        "invalidation, review_date, input_hash, prompt_versions, model_versions, reported, "
        "created_at) VALUES (1, 1, 'HOLD', 'positional_1_6m', 'low', '[]', 'd', 'h', '{}', "
        "'{}', 0, 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM run WHERE id = 1")


def test_backup_restore_carries_the_new_tables_and_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flip(src: Path, dst: Path, *_: object) -> None:
        dst.write_bytes(src.read_bytes()[::-1])

    monkeypatch.setattr(backup, "_encrypt", flip)
    monkeypatch.setattr(backup, "_decrypt", flip)
    d = tmp_path / "data"
    init_stores(d)
    c = sqlite3.connect(d / "nivesh.sqlite", isolation_level=None)
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'X', 'NSE', 'INR')")
    c.execute("INSERT INTO index_member VALUES ('N', 1, '2026-10-01')")
    c.execute("INSERT INTO watch VALUES (1, '1', '2', '2026-10-01')")
    c.close()
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    restored = tmp_path / "fresh"
    backup.restore_backup(out, restored, tmp_path / "identity.txt")
    r = sqlite3.connect(restored / "nivesh.sqlite")
    assert r.execute("SELECT index_id FROM index_member").fetchall() == [("N",)]
    assert r.execute("SELECT entry_high FROM watch").fetchall() == [("2",)]
    names = {x[0] for x in r.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert TRIGGERS <= names
