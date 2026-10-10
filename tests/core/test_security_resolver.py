import sqlite3
from pathlib import Path

import pytest

from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.security_resolver import Resolved, SecurityResolver, TableResolver


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path / "d")
    c = open_sqlite(tmp_path / "d" / "nivesh.sqlite")
    c.execute(
        "insert into security (symbol, exchange, name, isin, currency, asset_class, amfi_code) "
        "values ('RELI', 'NSE', 'Reliance', 'INE000A01010', 'INR', 'equity', 'A1')"
    )
    c.execute(
        "insert into security (symbol, exchange, isin, currency, unresolved) "
        "values ('INE999Z01019', 'ISIN', 'INE999Z01019', 'INR', 1)"
    )
    return c


def test_table_resolver_finds_seeded_isin(conn: sqlite3.Connection) -> None:
    assert TableResolver(conn).resolve("INE000A01010") is not None


def test_table_resolver_returns_none_for_unknown_isin(conn: sqlite3.Connection) -> None:
    assert TableResolver(conn).resolve("INE777Q01017") is None


def test_table_resolver_ignores_unresolved_placeholder_rows(conn: sqlite3.Connection) -> None:
    assert TableResolver(conn).resolve("INE999Z01019") is None


class DictResolver:
    def __init__(self, data: dict[str, Resolved]) -> None:
        self.data = data

    def resolve(self, isin: str) -> Resolved | None:
        return self.data.get(isin)


def need(resolver: SecurityResolver, isin: str) -> Resolved | None:
    return resolver.resolve(isin)


def test_in_memory_fake_satisfies_the_protocol() -> None:
    r = Resolved(symbol="X", name="X Ltd", exchange="NSE", asset_class="equity")
    assert need(DictResolver({"I": r}), "I") == r
    assert need(DictResolver({}), "I") is None


def test_resolved_carries_symbol_name_exchange_asset_class_and_amfi_code(
    conn: sqlite3.Connection,
) -> None:
    got = TableResolver(conn).resolve("INE000A01010")
    assert got == Resolved(
        symbol="RELI", name="Reliance", exchange="NSE", asset_class="equity", amfi_code="A1"
    )


def test_table_resolver_falls_back_to_isin_alias(conn: sqlite3.Connection) -> None:
    sid = conn.execute("select id from security where symbol = 'RELI'").fetchone()[0]
    conn.execute(
        "insert into security_alias (kind, value, security_id) values ('isin', 'INE555Q01010', ?)",
        (sid,),
    )
    got = TableResolver(conn).resolve("INE555Q01010")
    assert got is not None and got.symbol == "RELI"
    assert TableResolver(conn).resolve("INE556Q01010") is None


def test_alias_fallback_ignores_unresolved_placeholder_targets(conn: sqlite3.Connection) -> None:
    sid = conn.execute("select id from security where unresolved = 1").fetchone()[0]
    conn.execute(
        "insert into security_alias (kind, value, security_id) values ('isin', 'INE555Q01010', ?)",
        (sid,),
    )
    assert TableResolver(conn).resolve("INE555Q01010") is None
