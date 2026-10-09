import os
import sqlite3
from pathlib import Path


def open_sqlite(path: Path) -> sqlite3.Connection:
    """Open the transactional store, creating it 0600 (WAL sidecars inherit the mode)."""
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; migrations manage BEGIN
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
