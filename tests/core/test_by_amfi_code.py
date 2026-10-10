import sqlite3
from pathlib import Path

import pytest

from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.security_master import SecurityMaster, build_master
from tests.market_fx import mrow

G, P = "INF000A01011", "INF000A01029"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path / "d")
    return open_sqlite(tmp_path / "d" / "nivesh.sqlite")


def mf(isin: str, name: str, code: str = "100001") -> object:
    return mrow(isin, "AMFI", isin=isin, name=name, asset_class="mf", amfi_code=code)


def test_by_amfi_code_finds_row_prefers_growth_deterministically(conn: sqlite3.Connection) -> None:
    # the IDCW ISIN is listed first and gets the lower id
    build_master(
        conn,
        [mf(P, "Example Fund Direct IDCW Payout"), mf(G, "Example Fund Direct Growth")],  # type: ignore[list-item]
    )
    m = SecurityMaster(conn)
    row = m.by_amfi_code("100001")
    assert row is not None and row.isin == G
    assert m.by_amfi_code(" 100001 ") == row
    low = conn.execute("SELECT MIN(id) FROM security").fetchone()[0]
    assert row.id != low  # the growth row is chosen by name, not by id


def test_by_amfi_code_without_name_hint_uses_lowest_id(conn: sqlite3.Connection) -> None:
    build_master(conn, [mf(G, "Example Fund"), mf(P, "Example Fund")])  # type: ignore[list-item]
    row = SecurityMaster(conn).by_amfi_code("100001")
    assert row is not None and row.id == conn.execute("SELECT MIN(id) FROM security").fetchone()[0]


def test_by_amfi_code_unknown_is_none(conn: sqlite3.Connection) -> None:
    assert SecurityMaster(conn).by_amfi_code("999999") is None


def test_existing_lookup_contracts_unchanged(conn: sqlite3.Connection) -> None:
    build_master(conn, [mf(G, "Example Fund Growth")])  # type: ignore[list-item]
    m = SecurityMaster(conn)
    assert m.lookup(G).matched_by == "isin"
    assert m.resolve(G) is not None and m.resolve(G).amfi_code == "100001"  # type: ignore[union-attr]
    assert m.by_symbol(G)[0].asset_class == "mf"


def test_mf_schemes_one_row_per_code_growth_preferred(conn: sqlite3.Connection) -> None:
    build_master(
        conn,
        [
            mf(P, "Example Fund Direct IDCW", "100001"),  # type: ignore[list-item]
            mf(G, "Example Fund Direct Growth", "100001"),  # type: ignore[list-item]
            mf("INF999Z01011", "Other Fund Growth", "100002"),  # type: ignore[list-item]
            mrow("ABC", isin="INE000A01010"),
        ],
    )
    rows = SecurityMaster(conn).mf_schemes()
    assert [(c, r.isin, r.name) for c, r in rows] == [
        ("100001", G, "Example Fund Direct Growth"),
        ("100002", "INF999Z01011", "Other Fund Growth"),
    ]  # fmt: skip


def test_by_isin_exact_only_no_fuzzy_fallback(conn: sqlite3.Connection) -> None:
    build_master(conn, [mf(G, "Example Fund Growth")])  # type: ignore[list-item]
    m = SecurityMaster(conn)
    assert [r.isin for r in m.by_isin(G.lower())] == [G]
    assert m.by_isin("INF000ZZZ999") == []
    assert m.by_isin("Example Fund Growth") == []
