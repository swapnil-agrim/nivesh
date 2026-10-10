from pathlib import Path

import duckdb

from nivesh_core.paths import private_umask


def open_duck(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the analytical store; created under a private umask so it is 0600 from birth.

    DuckDB allows one read-write process or many read-only ones, never both.
    """
    with private_umask():
        return duckdb.connect(str(path), read_only=read_only)
