import sqlite3
from datetime import date
from pathlib import Path

import pytest

from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.errors import NiveshError
from nivesh_core.membership import (
    latest_as_of,
    load_members,
    members_of,
    parse_constituents,
)
from nivesh_core.security_master import SecurityMaster, build_master
from tests.market_fx import mrow

SQLITE = MIGRATIONS / "sqlite"
ASOF = date(2026, 10, 1)
CSV = """symbol,isin,sector
AAA,INE000A01010,Energy
BBB,INE000B01010,Banks
CCC,,Banks
ZZZ,INE999Z01010,Metals
"""


def master_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    build_master(
        c,
        [
            mrow("AAA", isin="INE000A01010"),
            mrow("BBB", isin="INE000B01010", sector="Finance"),
            mrow("CCC"),
            mrow("DDD", "NASDAQ", market="US", currency="USD"),
            mrow("CCC", "NASDAQ", market="US", currency="USD"),
        ],
        [],
    )
    return c


def test_parse_constituent_csv_symbol_isin_sector_columns() -> None:
    rows = parse_constituents(CSV)
    assert [(r.symbol, r.isin, r.sector) for r in rows] == [
        ("AAA", "INE000A01010", "Energy"),
        ("BBB", "INE000B01010", "Banks"),
        ("CCC", None, "Banks"),
        ("ZZZ", "INE999Z01010", "Metals"),
    ]
    assert parse_constituents("Symbol\nAAA\n\n")[0].isin is None  # header case, blank lines


def test_parse_rejects_missing_symbol_column_with_clear_error() -> None:
    with pytest.raises(NiveshError, match="symbol"):
        parse_constituents("ticker,isin\nAAA,x\n")
    with pytest.raises(NiveshError, match="no rows"):
        parse_constituents("symbol,isin\n")
    with pytest.raises(NiveshError, match="empty"):
        parse_constituents("")


def test_members_resolve_isin_first_then_symbol_alias() -> None:
    c = master_db()
    rep = load_members(c, SecurityMaster(c), "NIFTY500", "IN", parse_constituents(CSV), ASOF)
    ids = {r[0]: r[1] for r in c.execute("SELECT symbol, id FROM security WHERE market = 'IN'")}
    assert members_of(c, "NIFTY500") == sorted([ids["AAA"], ids["BBB"], ids["CCC"]])
    assert rep.loaded == 3
    # an ISIN match wins even when the symbol column names something else
    c2 = master_db()
    rows = parse_constituents("symbol,isin\nWRONG,INE000A01010\n")
    assert load_members(c2, SecurityMaster(c2), "X", "IN", rows, ASOF).loaded == 1


def test_symbol_resolution_stays_inside_the_index_market() -> None:
    c = master_db()  # CCC exists in IN and US
    load_members(c, SecurityMaster(c), "SP500", "US", parse_constituents("symbol\nCCC\n"), ASOF)
    (sid,) = members_of(c, "SP500")
    assert c.execute("SELECT market FROM security WHERE id = ?", (sid,)).fetchone() == ("US",)


def test_unresolved_rows_are_reported_with_symbols_not_dropped() -> None:
    c = master_db()
    rep = load_members(c, SecurityMaster(c), "NIFTY500", "IN", parse_constituents(CSV), ASOF)
    assert rep.unresolved == ["ZZZ"] and rep.total == 4 and rep.loaded == 3


def test_sector_from_the_file_fills_a_missing_master_sector_only() -> None:
    c = master_db()
    load_members(c, SecurityMaster(c), "NIFTY500", "IN", parse_constituents(CSV), ASOF)
    got = dict(c.execute("SELECT symbol, sector FROM security WHERE market = 'IN'").fetchall())
    assert got == {"AAA": "Energy", "BBB": "Finance", "CCC": "Banks"}


def test_reload_same_index_replaces_the_snapshot_atomically() -> None:
    c = master_db()
    m = SecurityMaster(c)
    load_members(c, m, "NIFTY500", "IN", parse_constituents(CSV), ASOF)
    load_members(c, m, "OTHER", "IN", parse_constituents("symbol\nAAA\n"), ASOF)
    later = date(2026, 11, 1)
    load_members(c, m, "NIFTY500", "IN", parse_constituents("symbol\nAAA\n"), later)
    assert len(members_of(c, "NIFTY500")) == 1 and latest_as_of(c, "NIFTY500") == later
    assert len(members_of(c, "OTHER")) == 1
    # a load that resolves nothing is refused and leaves the old snapshot in place
    with pytest.raises(NiveshError, match="none of the 1 rows"):
        load_members(c, m, "NIFTY500", "IN", parse_constituents("symbol\nNOPE\n"), later)
    assert len(members_of(c, "NIFTY500")) == 1


def test_members_of_unknown_index_is_empty() -> None:
    c = master_db()
    assert members_of(c, "NOPE") == [] and latest_as_of(c, "NOPE") is None


def test_load_file_helper_reads_a_path(tmp_path: Path) -> None:
    from nivesh_core.membership import read_constituent_file

    p = tmp_path / "n.csv"
    p.write_text(CSV)
    assert len(read_constituent_file(p)) == 4
    with pytest.raises(NiveshError, match="cannot read"):
        read_constituent_file(tmp_path / "missing.csv")
