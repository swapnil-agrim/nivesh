"""Open both stores for reading, without a command context (the scheduler has none)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb

from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError

IN_USE = "the market database is in use by another process; retry shortly"


@dataclass
class ReadStores:
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection


@contextmanager
def open_read_stores(settings: Settings, *, writable_sql: bool = False) -> Iterator[ReadStores]:
    """The market store read-only and the transactional store with writes switched off (or on,
    for a job that appends rows). A market store held by another process is a `NiveshError`,
    which callers may retry."""
    data_dir = Path(settings.data_dir)
    init_stores(data_dir)
    try:
        duck = open_duck(data_dir / "nivesh.duckdb", read_only=True)
    except (duckdb.ConnectionException, duckdb.IOException):
        raise NiveshError(IN_USE) from None
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    if not writable_sql:
        sql.execute("PRAGMA query_only = ON")
    try:
        yield ReadStores(sql, duck)
    finally:
        sql.close()
        duck.close()


@contextmanager
def open_sql(settings: Settings, *, writable: bool = False) -> Iterator[sqlite3.Connection]:
    """Only the transactional store; with writes switched off unless `writable`."""
    data_dir = Path(settings.data_dir)
    init_stores(data_dir)
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    if not writable:
        sql.execute("PRAGMA query_only = ON")
    try:
        yield sql
    finally:
        sql.close()
