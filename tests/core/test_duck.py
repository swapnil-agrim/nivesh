from pathlib import Path

import duckdb
import pytest

from nivesh_core.db.duck import open_duck


def test_open_duck_read_only_rejects_writes_and_reads_existing_rows(tmp_path: Path) -> None:
    p = tmp_path / "t.duckdb"
    w = open_duck(p)
    w.execute("CREATE TABLE t (a INTEGER)")
    w.execute("INSERT INTO t VALUES (7)")
    w.close()
    r = open_duck(p, read_only=True)
    assert r.execute("SELECT a FROM t").fetchone() == (7,)
    with pytest.raises(duckdb.Error):
        r.execute("INSERT INTO t VALUES (8)")
    r.close()
