import sqlite3
from pathlib import Path

import pytest

from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.security_resolver import (
    US_EXCHANGE_ALIASES,
    SecurityResolver,
    TableResolver,
    UsSymbolResolver,
    normalise_us_exchange,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path / "d")
    return open_sqlite(tmp_path / "d" / "nivesh.sqlite")


def add(
    conn: sqlite3.Connection, symbol: str, exchange: str, *, market: str = "US",
    asset_class: str = "equity", unresolved: int = 0, isin: str | None = None,
) -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO security (symbol, exchange, name, isin, currency, asset_class, market, "
        "unresolved) VALUES (?, ?, ?, ?, 'USD', ?, ?, ?)",
        (symbol, exchange, f"{symbol} Corp", isin, asset_class, market, unresolved),
    )


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("NYSE", "NYSE"), ("nasdaq", "NASDAQ"), ("ARCA", "ARCA"), ("NYSE ARCA", "ARCA"),
        ("NYSEARCA", "ARCA"), ("AMEX", "NYSE"), ("NYSE American", "NYSE"), (" Nasdaq ", "NASDAQ"),
        ("LSE", None), ("", None), ("CBOE", None),
    ],
)  # fmt: skip
def test_us_exchange_aliases_map_to_nyse_nasdaq_arca(raw: str, want: str | None) -> None:
    assert normalise_us_exchange(raw) == want
    assert set(US_EXCHANGE_ALIASES.values()) == {"NYSE", "NASDAQ", "ARCA"}


def test_resolve_symbol_finds_master_row_case_insensitively(conn: sqlite3.Connection) -> None:
    add(conn, "AAPL", "NASDAQ")
    got = UsSymbolResolver(conn).resolve_symbol("aapl")
    assert got is not None and (got.symbol, got.exchange, got.name) == (
        "AAPL", "NASDAQ", "AAPL Corp",
    )  # fmt: skip


def test_master_exchange_wins_over_csv_hint(conn: sqlite3.Connection) -> None:
    add(conn, "AAPL", "NASDAQ")
    got = UsSymbolResolver(conn).resolve_symbol("AAPL", "NYSE")
    assert got is not None and got.exchange == "NASDAQ"


def test_symbol_on_two_exchanges_needs_hint_else_none(conn: sqlite3.Connection) -> None:
    add(conn, "DUAL", "NYSE")
    add(conn, "DUAL", "NASDAQ")
    r = UsSymbolResolver(conn)
    assert r.resolve_symbol("DUAL") is None
    got = r.resolve_symbol("DUAL", "nasdaq")
    assert got is not None and got.exchange == "NASDAQ"
    assert r.resolve_symbol("DUAL", "ARCA") is None


def test_cboe_and_otc_rows_resolve_but_are_flagged_by_exchange(conn: sqlite3.Connection) -> None:
    add(conn, "CBX", "CBOE")
    add(conn, "OTCX", "OTC")
    add(conn, "FALL", "US")
    r = UsSymbolResolver(conn)
    for sym, exch in (("CBX", "CBOE"), ("OTCX", "OTC"), ("FALL", "US")):
        got = r.resolve_symbol(sym)
        assert got is not None and got.exchange == exch
        assert normalise_us_exchange(got.exchange) is None


def test_unresolved_placeholders_india_and_index_rows_ignored(conn: sqlite3.Connection) -> None:
    add(conn, "PLC", "ISIN", unresolved=1)
    add(conn, "RELI", "NSE", market="IN")
    add(conn, "^GSPC", "INDEX", asset_class="index")
    r = UsSymbolResolver(conn)
    assert [r.resolve_symbol(s) for s in ("PLC", "RELI", "^GSPC")] == [None, None, None]


def test_symbol_lookup_folds_dot_and_dash(conn: sqlite3.Connection) -> None:
    add(conn, "BRK-B", "NYSE")
    r = UsSymbolResolver(conn)
    for q in ("BRK.B", "brk-b", "BRK-B"):
        got = r.resolve_symbol(q)
        assert got is not None and got.symbol == "BRK-B"


def test_table_resolver_and_security_master_isin_contract_unchanged(
    conn: sqlite3.Connection,
) -> None:
    add(conn, "AAPL", "NASDAQ", isin="US0378331005")
    r: SecurityResolver = TableResolver(conn)
    got = r.resolve("US0378331005")
    assert got is not None and got.symbol == "AAPL"
    assert r.resolve("US0000000000") is None


def test_row_with_null_asset_class_still_resolves(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO security (symbol, exchange, currency, market) "
        "VALUES ('NULLC', 'NYSE', 'USD', 'US')"
    )
    got = UsSymbolResolver(conn).resolve_symbol("NULLC")
    assert got is not None and got.asset_class == "equity"
