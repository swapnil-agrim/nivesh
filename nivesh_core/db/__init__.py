from pathlib import Path

import duckdb

from nivesh_core.db import migrate
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.paths import ensure_data_dir, private_umask

MIGRATIONS = Path(__file__).parent / "migrations"


def _duck_is_current(path: Path) -> bool:
    """True when DuckDB needs no read-write open: already at the latest version, or locked.

    A lock means a writer is active; every writer enters through `init_stores`, so the file is
    already current. Any other probe failure falls through to the read-write `apply` path.
    """
    if not path.exists():
        return False
    try:
        probe = open_duck(path, read_only=True)
    except (duckdb.ConnectionException, duckdb.IOException):
        return True
    except duckdb.Error:
        return False
    try:
        return migrate.current_version(probe) == migrate.latest_version(MIGRATIONS / "duck")
    except duckdb.Error:
        return False
    finally:
        probe.close()


def init_stores(data_dir: Path) -> None:
    """Create/upgrade both stores in an owner-only data dir. Idempotent.

    DuckDB is opened read-write only when it needs a change, so readers (MCP tools) never fight
    over its single-writer lock once the schema is current.
    """
    ensure_data_dir(data_dir)
    with private_umask():
        s = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            migrate.apply(s, MIGRATIONS / "sqlite")
        finally:
            s.close()
        if _duck_is_current(data_dir / "nivesh.duckdb"):
            return
        d = open_duck(data_dir / "nivesh.duckdb")
        try:
            migrate.apply(d, MIGRATIONS / "duck")
        finally:
            d.close()
