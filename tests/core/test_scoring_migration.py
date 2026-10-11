import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nivesh_core import backup
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from tests.core.test_backup import RECIPIENT
from tests.core.test_us_holdings_migration import upto

SQLITE = MIGRATIONS / "sqlite"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
TRIGGERS = {
    "ledger_entry_no_update", "ledger_entry_no_delete", "ledger_decision_no_update",
    "ledger_decision_no_delete", "ledger_score_no_update", "ledger_score_no_delete",
}  # fmt: skip
ENTRY = (
    "INSERT INTO ledger_entry (run_id, security_id, verdict, horizon, conviction, invalidation, "
    "review_date, input_hash, prompt_versions, model_versions, reported, created_at) VALUES "
    "(1, 1, 'BUY', 'positional_1_6m', 'low', '[]', 'd', 'h', '{}', '{}', 1, 't')"
)
SCORE = (
    "INSERT INTO ledger_score (entry_id, horizon_weeks, status, basis, scored_at) "
    "VALUES (1, ?, ?, 'split_adjusted_price', 't')"
)
DECISION = (
    "INSERT INTO ledger_decision (entry_id, decision, on_date, decided_at) VALUES (1, ?, 'd', 't')"
)


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'A', 'NSE', 'INR')")
    c.execute("INSERT INTO run (id, command, started_at, status) VALUES (1, 'ideas', 't', 'ok')")
    c.execute(ENTRY)
    return c


def test_columns_and_tables_exist_and_old_rows_default() -> None:
    c = fresh()
    cols = {r[1] for r in c.execute("PRAGMA table_info(ledger_entry)")}
    assert {"as_of", "price_date", "source"} <= cols
    assert c.execute("SELECT as_of, price_date, source FROM ledger_entry").fetchone() == (
        None, None, "verdict",
    )  # fmt: skip
    for t in ("ledger_decision", "ledger_score"):
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone() == (0,)  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO ledger_entry (run_id, security_id, verdict, horizon, conviction, "
            "invalidation, review_date, input_hash, prompt_versions, model_versions, reported, "
            "created_at, source) VALUES (1, 1, 'BUY', 'positional_1_6m', 'low', '[]', 'd', 'h', "
            "'{}', '{}', 1, 't', 'other')"
        )


@pytest.mark.parametrize("table", ["ledger_decision", "ledger_score", "ledger_entry"])
def test_update_and_delete_are_refused(table: str) -> None:
    c = fresh()
    c.execute(DECISION, ("acted",))
    c.execute(SCORE, (1, "scored"))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(f"UPDATE {table} SET id = id")  # noqa: S608
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(f"DELETE FROM {table}")  # noqa: S608
    assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)  # noqa: S608


def test_one_scored_row_per_key_but_scored_may_follow_insufficient() -> None:
    c = fresh()
    c.execute(SCORE, (4, "insufficient_data"))
    c.execute(SCORE, (4, "insufficient_data"))
    c.execute(SCORE, (4, "scored"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(SCORE, (4, "scored"))
    c.execute(SCORE, (13, "scored"))


def test_bad_values_and_foreign_keys_rejected() -> None:
    c = fresh()
    for bad in (("maybe",), ("",)):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(DECISION, bad)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(SCORE, (2, "scored"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(SCORE, (4, "pending"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO ledger_decision (entry_id, decision, on_date, decided_at) "
                  "VALUES (99, 'acted', 'd', 't')")  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO ledger_score (entry_id, horizon_weeks, status, basis, scored_at) "
                  "VALUES (99, 4, 'scored', 'b', 't')")  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM run WHERE id = 1")


def test_migration_on_a_0007_store_keeps_rows(tmp_path: Path) -> None:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, upto(tmp_path, 7))
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'A', 'NSE', 'INR')")
    c.execute("INSERT INTO run (id, command, started_at, status) VALUES (1, 'ideas', 't', 'ok')")
    c.execute(ENTRY)
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == 8
    assert c.execute("SELECT id, source, as_of FROM ledger_entry").fetchall() == [
        (1, "verdict", None)
    ]


def test_triggers_survive_backup_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flip(src: Path, dst: Path, *_: object) -> None:
        dst.write_bytes(src.read_bytes()[::-1])

    monkeypatch.setattr(backup, "_encrypt", flip)
    monkeypatch.setattr(backup, "_decrypt", flip)
    d = tmp_path / "data"
    init_stores(d)
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    restored = tmp_path / "fresh"
    backup.restore_backup(out, restored, tmp_path / "identity.txt")
    r = sqlite3.connect(restored / "nivesh.sqlite")
    names = {x[0] for x in r.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert TRIGGERS <= names
