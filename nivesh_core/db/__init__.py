from pathlib import Path

from nivesh_core.db import migrate
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.paths import ensure_data_dir, private_umask

MIGRATIONS = Path(__file__).parent / "migrations"


def init_stores(data_dir: Path) -> None:
    """Create/upgrade both stores in an owner-only data dir. Idempotent."""
    ensure_data_dir(data_dir)
    with private_umask():
        s = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            migrate.apply(s, MIGRATIONS / "sqlite")
        finally:
            s.close()
        d = open_duck(data_dir / "nivesh.duckdb")
        try:
            migrate.apply(d, MIGRATIONS / "duck")
        finally:
            d.close()
