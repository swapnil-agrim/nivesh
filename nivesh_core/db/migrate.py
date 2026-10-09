"""Forward-only SQL migrations: `NNNN_name.sql`, one transaction per file, version row per file."""

import re
import sqlite3
from pathlib import Path
from typing import Any

from nivesh_core.errors import NiveshError
from nivesh_core.timeutil import to_iso, utcnow

_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(NiveshError):
    pass


def _files(directory: Path) -> list[tuple[int, Path]]:
    found = sorted(
        (int(m.group(1)), p) for p in directory.glob("*.sql") if (m := _FILE_RE.match(p.name))
    )
    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"{directory}: duplicate migration version")
    return found


def latest_version(directory: Path) -> int:
    return max((v for v, _ in _files(directory)), default=0)


def current_version(conn: Any) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


def _statements(sql: str) -> list[str]:
    """Split a script on statement boundaries (sqlite3.executescript would commit implicitly)."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return out


def apply(conn: Any, directory: Path) -> None:
    """Bring `conn` (sqlite3 in autocommit mode, or duckdb) up to the latest version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    files = _files(directory)
    have = current_version(conn)
    if have > (files[-1][0] if files else 0):
        raise MigrationError(
            f"database schema v{have} is newer than this code; refusing to downgrade"
        )
    for version, path in files:
        if version <= have:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in _statements(path.read_text()):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version VALUES (?, ?, ?)",
                (version, path.stem, to_iso(utcnow())),
            )
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise MigrationError(f"migration {path.stem} failed: {e}") from e
