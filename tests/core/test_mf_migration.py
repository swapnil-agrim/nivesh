import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

import nivesh_core.db as db
from nivesh_core.db import MIGRATIONS, init_stores, migrate

DUCK = MIGRATIONS / "duck"
MF_TABLES = {"nav_point", "fund_meta", "fund_holding", "nav_gap"}


def upto(tmp_path: Path, n: int) -> Path:
    d = tmp_path / f"v{n}"
    d.mkdir()
    for f in sorted(DUCK.glob("*.sql")):
        if int(f.name[:4]) <= n:
            shutil.copy(f, d / f.name)
    return d


def test_duck_0003_upgrades_v2_db_keeping_market_rows(tmp_path: Path) -> None:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, upto(tmp_path, 2))
    c.execute("INSERT INTO price_bar VALUES (1, '2026-01-05', 1, 2, 1, 2, 10, 2, 'nse', NULL, 't')")
    c.execute("INSERT INTO macro_series VALUES ('S', '2026-01-05', 1.5, 'fred')")
    migrate.apply(c, DUCK)
    assert migrate.current_version(c) == migrate.latest_version(DUCK) >= 3
    assert c.execute("SELECT close FROM price_bar").fetchone() == (Decimal("2.000000"),)
    assert c.execute("SELECT series_id FROM macro_series").fetchone() == ("S",)
    have = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert MF_TABLES <= have


def test_mf_tables_exist_with_decimal_columns(tmp_path: Path) -> None:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, DUCK)
    c.execute("INSERT INTO nav_point VALUES (1, '2026-01-05', 123.4567, 'mfapi', 't')")
    c.execute(
        "INSERT INTO fund_meta VALUES (1, '2026-01-05', '100001', 'n', NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, 'x')"
    )
    assert c.execute("SELECT nav FROM nav_point").fetchone() == (Decimal("123.4567"),)
    assert c.execute("SELECT expense_ratio, aum_crore FROM fund_meta").fetchone() == (None, None)
    types = dict(
        c.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name IN ('nav_point', 'fund_meta', 'fund_holding')"
        ).fetchall()
    )
    for col in ("nav", "expense_ratio", "aum_crore", "weight_pct"):
        assert types[col].startswith("DECIMAL"), col


def test_0003_rerun_is_noop(tmp_path: Path) -> None:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, DUCK)
    c.execute("INSERT INTO nav_point VALUES (1, '2026-01-05', 10, 'mfapi', 't')")
    migrate.apply(c, DUCK)
    assert c.execute("SELECT count(*) FROM nav_point").fetchone() == (1,)


def test_init_stores_upgrades_existing_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    old = tmp_path / "mig"
    shutil.copytree(MIGRATIONS, old)
    (old / "duck" / "0003_mf.sql").unlink()
    monkeypatch.setattr(db, "MIGRATIONS", old)
    init_stores(data)
    with duckdb.connect(str(data / "nivesh.duckdb")) as k:
        assert migrate.current_version(k) == 2
        k.execute(
            "INSERT INTO price_bar VALUES (1, ?, 1, 2, 1, 2, 10, 2, 'nse', NULL, 't')",
            (date(2026, 1, 5),),
        )
    monkeypatch.undo()
    init_stores(data)
    with duckdb.connect(str(data / "nivesh.duckdb"), read_only=True) as k:
        assert migrate.current_version(k) == migrate.latest_version(DUCK)
        assert k.execute("SELECT count(*) FROM price_bar").fetchone() == (1,)
        k.execute("SELECT * FROM nav_point")
