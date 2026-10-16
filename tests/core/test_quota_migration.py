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
    "vendor_call_accounting_is_fixed", "vendor_call_outcome_is_set_once", "vendor_call_no_delete",
    "vendor_call_no_replace", "vendor_limit_no_update", "vendor_limit_no_delete",
    "vendor_limit_no_replace",
}  # fmt: skip
CALL = (
    "INSERT INTO vendor_call (id, vendor, called_at, day_ist, tool, symbol, priority) "
    "VALUES (?, 'v', '2026-10-09T12:00:00+00:00', '2026-10-09', 't', 'S', 'universe')"
)
LIMIT = (
    "INSERT INTO vendor_limit (id, vendor, daily, monthly, set_at) VALUES (?, 'v', 5, NULL, 't')"
)
BAD_CALL = "INSERT INTO vendor_call (vendor, called_at, day_ist, {}) VALUES ('v', 't', 'd', {})"
ACCOUNTING = {
    "id": "99", "vendor": "'w'", "called_at": "'t'", "day_ist": "'d'", "kind": "'manual'",
    "calls": "2", "tool": "'u'", "symbol": "'T'", "priority": "'holdings'",
}  # fmt: skip


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    return c


def test_tables_columns_and_version() -> None:
    c = fresh()
    cols = {
        t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}  # noqa: S608
        for t in ("vendor_call", "vendor_limit")
    }
    assert cols["vendor_call"] == {
        "id", "vendor", "called_at", "day_ist", "kind", "calls", "tool", "symbol", "priority",
        "outcome",
    }  # fmt: skip
    assert cols["vendor_limit"] == {"id", "vendor", "daily", "monthly", "set_at"}
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 10
    index = c.execute("PRAGMA index_info(vendor_call_by_day)").fetchall()
    assert [r[2] for r in index] == ["vendor", "day_ist"]
    assert c.execute("SELECT kind, calls, outcome FROM vendor_call").fetchall() == []


def test_defaults_are_a_single_unsettled_call() -> None:
    c = fresh()
    c.execute(CALL, (1,))
    assert c.execute("SELECT kind, calls, outcome FROM vendor_call").fetchone() == (
        "call", 1, None,
    )  # fmt: skip


def test_outcome_can_be_set_exactly_once() -> None:
    c = fresh()
    c.execute(CALL, (1,))
    c.execute("UPDATE vendor_call SET outcome = 'ok' WHERE id = 1")
    assert c.execute("SELECT outcome FROM vendor_call").fetchone() == ("ok",)
    for again in ("ok", "error", "halted", None):
        with pytest.raises(sqlite3.DatabaseError, match="set once"):
            c.execute("UPDATE vendor_call SET outcome = ?", (again,))
    assert c.execute("SELECT outcome FROM vendor_call").fetchone() == ("ok",)


@pytest.mark.parametrize("outcome", ["ok", "error", "halted"])
def test_each_outcome_is_accepted_once(outcome: str) -> None:
    c = fresh()
    c.execute(CALL, (1,))
    c.execute("UPDATE vendor_call SET outcome = ? WHERE id = 1", (outcome,))
    assert c.execute("SELECT outcome FROM vendor_call").fetchone() == (outcome,)


@pytest.mark.parametrize("column", sorted(ACCOUNTING))
def test_accounting_columns_cannot_change(column: str) -> None:
    c = fresh()
    c.execute(CALL, (1,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(f"UPDATE vendor_call SET {column} = {ACCOUNTING[column]}")  # noqa: S608
    # even when the same statement also sets the outcome for the first time
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(f"UPDATE vendor_call SET outcome = 'ok', {column} = {ACCOUNTING[column]}")  # noqa: S608
    assert c.execute("SELECT outcome, calls, vendor FROM vendor_call").fetchone() == (None, 1, "v")


def test_vendor_call_rows_cannot_be_deleted_or_replaced() -> None:
    c = fresh()
    c.execute(CALL, (1,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM vendor_call")
    for verb in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(CALL.replace("INSERT INTO", verb), (1,))
    c.execute(CALL, (2,))  # a new key is fine
    assert c.execute("SELECT count(*) FROM vendor_call").fetchone() == (2,)


@pytest.mark.parametrize(
    "sql",
    [
        BAD_CALL.format("kind", "'x'"),
        BAD_CALL.format("calls", "0"),
        BAD_CALL.format("priority", "'x'"),
        BAD_CALL.format("outcome", "'x'"),
        "INSERT INTO vendor_call (called_at, day_ist) VALUES ('t', 'd')",  # no vendor
        "INSERT INTO vendor_call (vendor, day_ist) VALUES ('v', 'd')",  # no called_at
        "INSERT INTO vendor_call (vendor, called_at) VALUES ('v', 't')",  # no day_ist
        "INSERT INTO vendor_limit (vendor, daily, set_at) VALUES ('v', 0, 't')",
        "INSERT INTO vendor_limit (vendor, monthly, set_at) VALUES ('v', 0, 't')",
        "INSERT INTO vendor_limit (vendor, daily) VALUES ('v', 5)",  # no set_at
        "INSERT INTO vendor_limit (daily, set_at) VALUES (5, 't')",  # no vendor
    ],
)
def test_bad_values_are_rejected(sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        fresh().execute(sql)


def test_a_manual_row_and_null_priority_are_allowed() -> None:
    c = fresh()
    c.execute(
        "INSERT INTO vendor_call (vendor, called_at, day_ist, kind, calls) "
        "VALUES ('v', 't', '2026-10-09', 'manual', 120)"
    )
    assert c.execute("SELECT kind, calls, priority, tool FROM vendor_call").fetchone() == (
        "manual", 120, None, None,
    )  # fmt: skip


def test_vendor_limit_is_fully_append_only() -> None:
    c = fresh()
    c.execute(LIMIT, (1,))
    for sql in ("UPDATE vendor_limit SET daily = 6", "DELETE FROM vendor_limit"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(sql)
    for verb in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(LIMIT.replace("INSERT INTO", verb), (1,))
    c.execute(LIMIT, (2,))
    assert c.execute("SELECT count(*) FROM vendor_limit").fetchone() == (2,)


def test_migrating_a_version_9_store_keeps_its_rows(tmp_path: Path) -> None:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, upto(tmp_path, 9))
    assert migrate.current_version(c) == 9
    c.execute(
        "INSERT INTO job_run (job, trading_date, status, started_at) VALUES ('j', 'd', 'ok', 't')"
    )
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 10
    assert c.execute("SELECT job, status FROM job_run").fetchall() == [("j", "ok")]
    assert c.execute("SELECT count(*) FROM vendor_call").fetchone() == (0,)


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
