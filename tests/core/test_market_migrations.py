import sqlite3
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from nivesh_core.db import MIGRATIONS, init_stores, migrate

DUCK = MIGRATIONS / "duck"
SQLITE = MIGRATIONS / "sqlite"
TABLES = {
    "price_bar", "corp_action", "fundamental", "shareholding", "estimate", "macro_series",
    "filing", "filing_section", "news_item", "calendar_event",
}  # fmt: skip


def duck(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, DUCK)
    return c


def test_duck_0002_creates_market_tables_and_keeps_cache_entry_rows(tmp_path: Path) -> None:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    only_v1 = tmp_path / "v1"
    only_v1.mkdir()
    (only_v1 / "0001_cache.sql").write_text((DUCK / "0001_cache.sql").read_text())
    migrate.apply(c, only_v1)
    c.execute("INSERT INTO cache_entry VALUES ('a', 'h', '{}', 's', 't', 't')")
    migrate.apply(c, DUCK)
    assert migrate.current_version(c) == migrate.latest_version(DUCK) >= 2
    assert c.execute("SELECT adapter FROM cache_entry").fetchone() == ("a",)
    have = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert TABLES <= have


def _bar(c: duckdb.DuckDBPyConnection, source: str, day: str = "2026-01-05") -> None:
    c.execute("INSERT INTO price_bar VALUES (1, ?, 1, 2, 1, 2, 10, 2, ?, NULL, 't')", (day, source))


def test_duck_price_bar_primary_key_allows_one_bar_per_source_per_day(tmp_path: Path) -> None:
    c = duck(tmp_path)
    _bar(c, "nse")
    _bar(c, "yahoo")
    _bar(c, "nse", "2026-01-06")
    assert c.execute("SELECT count(*) FROM price_bar").fetchone() == (3,)


def test_duplicate_bar_same_source_rejected(tmp_path: Path) -> None:
    c = duck(tmp_path)
    _bar(c, "nse")
    with pytest.raises(duckdb.ConstraintException):
        _bar(c, "nse")


def test_duck_fundamental_is_append_only_across_filed_at(tmp_path: Path) -> None:
    c = duck(tmp_path)
    for filed in ("2026-02-01", "2026-03-01"):
        c.execute(
            "INSERT INTO fundamental VALUES "
            "(1, '2025-12-31', 'Q', 'revenue', 5, 'USD', 'edgar', ?, 't')",
            (filed,),
        )
    assert c.execute("SELECT count(*) FROM fundamental").fetchone() == (2,)


def test_duck_news_and_filing_ids_come_from_sequences(tmp_path: Path) -> None:
    c = duck(tmp_path)
    for _ in range(2):
        c.execute(
            "INSERT INTO news_item (kind, published_at, source, title) VALUES ('news','t','s','x')"
        )
        c.execute(
            "INSERT INTO filing (security_id, form, filed_at, source, doc_key) "
            "VALUES (1, '10-K', '2026-01-01', 's', uuid()::VARCHAR)"
        )
    assert c.execute("SELECT id FROM news_item ORDER BY id").fetchall() == [(1,), (2,)]
    assert c.execute("SELECT id FROM filing ORDER BY id").fetchall() == [(1,), (2,)]


def test_duck_decimal_columns_round_trip_exactly(tmp_path: Path) -> None:
    c = duck(tmp_path)
    c.execute(
        "INSERT INTO price_bar VALUES (1, '2026-01-05', ?, ?, ?, ?, 1, ?, 'x', NULL, 't')",
        [Decimal("1234.567891")] * 3 + [Decimal("1234.567891")] * 2,
    )
    assert c.execute("SELECT close, adj_close FROM price_bar").fetchone() == (
        Decimal("1234.567891"),
        Decimal("1234.567891"),
    )


def test_sqlite_0004_upgrades_v3_database_keeping_security_rows(tmp_path: Path) -> None:
    c = sqlite3.connect(":memory:", isolation_level=None)
    three = tmp_path / "v3"
    three.mkdir()
    for f in sorted(SQLITE.glob("000[123]_*.sql")):
        (three / f.name).write_text(f.read_text())
    migrate.apply(c, three)
    c.execute("INSERT INTO security (symbol, exchange, currency) VALUES ('AAA', 'NSE', 'INR')")
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 4
    row = c.execute("SELECT symbol, sector, industry, name_norm FROM security").fetchone()
    assert row == ("AAA", None, None, None)


def test_sqlite_0004_creates_master_merge_log_and_alias_pk(tmp_path: Path) -> None:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    c.execute("INSERT INTO master_merge_log VALUES (1, 1, 2, 't', '{}', 'now')")
    c.execute("INSERT INTO security (symbol, exchange, currency) VALUES ('AAA', 'NSE', 'INR')")
    c.execute("INSERT INTO security_alias VALUES ('isin', 'X', 1, NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO security_alias VALUES ('isin', 'X', 1, NULL)")
    with pytest.raises(sqlite3.IntegrityError):  # alias requires an existing security
        c.execute("INSERT INTO security_alias VALUES ('isin', 'Y', 99, NULL)")


def test_both_migrations_rerun_is_noop_and_init_creates_latest(tmp_path: Path) -> None:
    d = tmp_path / "data"
    init_stores(d)
    init_stores(d)
    s = sqlite3.connect(d / "nivesh.sqlite")
    assert migrate.current_version(s) == migrate.latest_version(SQLITE)
    s.close()
    with duckdb.connect(str(d / "nivesh.duckdb"), read_only=True) as k:
        assert migrate.current_version(k) == migrate.latest_version(DUCK)
