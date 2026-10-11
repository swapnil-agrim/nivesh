import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nivesh_core import backup
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from tests.core.test_backup import RECIPIENT

SQLITE = MIGRATIONS / "sqlite"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
TRIGGERS = {
    "alert_no_update", "alert_no_delete", "alert_no_replace",
    "invalidation_level_no_update", "invalidation_level_no_delete",
    "invalidation_level_no_replace",
}  # fmt: skip
ALERT = (
    "INSERT INTO alert (alert_id, rule, security_id, fired_at, trading_date, payload, delivered) "
    "VALUES (?, 'big_move', 1, 't', 'd', '{}', 0)"
)
LEVEL = "INSERT INTO invalidation_level (id, security_id, level, set_on) VALUES (?, 1, '90', 'd')"
JOB = "INSERT INTO job_run (job, trading_date, status, started_at) VALUES ('brief_in', ?, ?, 't')"


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'A', 'NSE', 'INR')")
    return c


def test_tables_and_columns_exist() -> None:
    c = fresh()
    cols = {t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")} for t in (  # noqa: S608
        "job_run", "alert", "invalidation_level")}  # fmt: skip
    assert {"job", "trading_date", "status", "attempts", "run_id", "started_at", "finished_at",
            "reason", "late", "notified"} <= cols["job_run"]  # fmt: skip
    assert {"alert_id", "rule", "security_id", "fired_at", "trading_date", "payload", "delivered",
            "digest_id"} <= cols["alert"]  # fmt: skip
    assert {"id", "security_id", "level", "set_on", "note"} <= cols["invalidation_level"]
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 9


@pytest.mark.parametrize(("table", "insert"), [("alert", ALERT), ("invalidation_level", LEVEL)])
def test_append_only_refuses_update_delete_and_replace(table: str, insert: str) -> None:
    c = fresh()
    c.execute(insert, (1,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(f"UPDATE {table} SET security_id = security_id")  # noqa: S608
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(f"DELETE FROM {table}")  # noqa: S608
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(insert.replace("INSERT INTO", "INSERT OR REPLACE INTO"), (1,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute(insert.replace("INSERT INTO", "REPLACE INTO"), (1,))
    c.execute(insert, (2,))  # a new key is fine
    assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (2,)  # noqa: S608


def test_job_run_key_is_unique_but_rows_are_mutable() -> None:
    c = fresh()
    c.execute(JOB, ("2026-10-09", "running"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(JOB, ("2026-10-09", "ok"))
    c.execute(JOB, ("2026-10-10", "running"))
    c.execute("UPDATE job_run SET status = 'ok', attempts = 1 WHERE trading_date = '2026-10-09'")
    assert c.execute("SELECT status, attempts, late, notified FROM job_run ORDER BY 1, trading_date"
                     ).fetchone() == ("ok", 1, 0, 0)  # fmt: skip


def test_bad_values_and_foreign_keys_rejected() -> None:
    c = fresh()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(JOB, ("2026-10-09", "done"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO alert (rule, security_id, fired_at, trading_date, payload, "
                  "delivered) VALUES ('r', 1, 't', 'd', '{}', 3)")  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO alert (rule, security_id, fired_at, trading_date, payload, "
                  "delivered) VALUES ('r', 99, 't', 'd', '{}', 0)")  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO invalidation_level (security_id, level, set_on) "
                  "VALUES (99, '1', 'd')")  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO job_run (job, trading_date, status, started_at, run_id) "
                  "VALUES ('j', 'd', 'ok', 't', 99)")  # fmt: skip


def test_alert_without_security_is_allowed() -> None:
    c = fresh()
    c.execute("INSERT INTO alert (rule, fired_at, trading_date, payload, delivered) "
              "VALUES ('digest', 't', 'd', '{}', 1)")  # fmt: skip
    assert c.execute("SELECT security_id FROM alert").fetchone() == (None,)


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
