from pathlib import Path

import duckdb

from nivesh_core.paths import private_umask


def open_duck(path: Path) -> duckdb.DuckDBPyConnection:
    """Open the analytical store; created under a private umask so it is 0600 from birth."""
    with private_umask():
        return duckdb.connect(str(path))
