from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.mf_models import FundHoldingRow, FundMeta, NavGap, NavPoint
from nivesh_core.mf_store import (
    get_fund_holdings,
    get_gaps,
    get_nav,
    last_nav_date,
    latest_fund_meta,
    months_stored,
    nav_by_source,
    upsert_nav,
    write_fund_holdings,
    write_fund_meta,
    write_gaps,
)

D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, MIGRATIONS / "duck")
    return c


def pt(day: int, nav: str, source: str = "mfapi") -> NavPoint:
    return NavPoint(date=date(2026, 1, day), nav=D(nav), source=source)


def meta(as_of: date, **kw: object) -> FundMeta:
    base: dict[str, object] = {
        "as_of": as_of, "amfi_code": "100001", "scheme_name": "Example Fund", "source": "mf_meta"
    }  # fmt: skip
    base.update(kw)
    return FundMeta(**base)  # type: ignore[arg-type]


def test_upsert_nav_round_trip_exact_decimal(db: duckdb.DuckDBPyConnection) -> None:
    upsert_nav(db, 7, [pt(5, "123.4567"), pt(6, "0.0001")])
    upsert_nav(db, 7, [pt(5, "123.4567")])  # idempotent
    got = get_nav(db, 7)
    assert [p.nav for p in got] == [D("123.4567"), D("0.0001")]
    assert db.execute("SELECT count(*) FROM nav_point").fetchone() == (2,)


def test_get_nav_oldest_first_one_value_per_date_primary_wins(
    db: duckdb.DuckDBPyConnection,
) -> None:
    upsert_nav(
        db,
        7,
        [pt(6, "11"), pt(5, "10"), pt(5, "99", "amfi_navall"), pt(7, "12", "amfi_navall")],
        fetched_at=datetime(2026, 1, 8, tzinfo=UTC),
    )
    got = get_nav(db, 7)
    assert [(p.date.day, p.nav, p.source) for p in got] == [
        (5, D("10"), "mfapi"), (6, D("11"), "mfapi"), (7, D("12"), "amfi_navall"),
    ]  # fmt: skip
    assert [p.date.day for p in get_nav(db, 7, date(2026, 1, 6), date(2026, 1, 6))] == [6]
    assert set(nav_by_source(db, 7)) == {"mfapi", "amfi_navall"}
    assert get_nav(db, 8) == []


def test_last_nav_date_none_when_empty(db: duckdb.DuckDBPyConnection) -> None:
    assert last_nav_date(db, 7) is None
    upsert_nav(db, 7, [pt(5, "10"), pt(9, "10")])
    assert last_nav_date(db, 7) == date(2026, 1, 9)


def test_fund_meta_append_only_by_as_of_latest_wins(db: duckdb.DuckDBPyConnection) -> None:
    write_fund_meta(db, 7, meta(date(2026, 1, 1), expense_ratio=D("1.5000")))
    write_fund_meta(db, 7, meta(date(2026, 2, 1), expense_ratio=D("1.2500"), plan="direct"))
    assert db.execute("SELECT count(*) FROM fund_meta").fetchone() == (2,)
    latest = latest_fund_meta(db, 7)
    assert latest is not None and latest.expense_ratio == D("1.2500") and latest.plan == "direct"
    old = latest_fund_meta(db, 7, date(2026, 1, 15))
    assert old is not None and old.expense_ratio == D("1.5000")
    assert latest_fund_meta(db, 7, date(2025, 1, 1)) is None
    assert latest_fund_meta(db, 8) is None


def test_fund_meta_null_ter_stays_null_not_zero(db: duckdb.DuckDBPyConnection) -> None:
    write_fund_meta(db, 7, meta(date(2026, 1, 1)))
    m = latest_fund_meta(db, 7)
    assert m is not None and m.expense_ratio is None and m.aum_crore is None
    assert m.manager_since is None


def test_fund_holdings_round_trip_and_months_stored(db: duckdb.DuckDBPyConnection) -> None:
    rows = [
        FundHoldingRow(
            month_end=date(2025, m, 28), isin=i, weight_pct=D(w), kind=k, source="fixture",
            holding_security_id=hs,
        )
        for m in (1, 2)
        for i, w, k, hs in (("INF000A01011", "60.5", "equity", 3), ("CASH", "3", "other", None))
    ]  # fmt: skip
    write_fund_holdings(db, 7, rows)
    write_fund_holdings(db, 7, rows)
    got = get_fund_holdings(db, 7, date(2025, 2, 28))
    assert [(r.isin, r.weight_pct, r.kind, r.holding_security_id) for r in got] == [
        ("INF000A01011", D("60.5000"), "equity", 3), ("CASH", D("3.0000"), "other", None),
    ]  # fmt: skip
    assert months_stored(db, 7) == [date(2025, 1, 28), date(2025, 2, 28)]
    assert months_stored(db, 8) == []
    write_fund_holdings(db, 7, [])


def test_nav_gap_roundtrip(db: duckdb.DuckDBPyConnection) -> None:
    g1 = NavGap(gap_start=date(2026, 1, 5), gap_end=date(2026, 1, 14), missing_days=6)
    write_gaps(db, 7, [g1])
    assert get_gaps(db, 7) == [g1]
    write_gaps(db, 7, [])  # recompute-and-replace: a backfill clears the flag
    assert get_gaps(db, 7) == []


def test_models_are_frozen_and_reject_non_positive_nav() -> None:
    with pytest.raises(ValidationError):
        NavPoint(date=date(2026, 1, 5), nav=D("0"), source="x")
    with pytest.raises(ValidationError):
        NavPoint(date=date(2026, 1, 5), nav=D("-1"), source="x")
    p = pt(5, "10")
    with pytest.raises(ValidationError):
        p.nav = D("11")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FundHoldingRow(month_end=date(2026, 1, 31), isin="X", weight_pct=D("101"), source="s")


def test_rewriting_a_month_replaces_it_so_dropped_lines_do_not_linger(
    db: duckdb.DuckDBPyConnection,
) -> None:
    def row(isin: str) -> FundHoldingRow:
        return FundHoldingRow(
            month_end=date(2025, 1, 31), isin=isin, weight_pct=D("5"), source="mf_holdings"
        )

    write_fund_holdings(db, 7, [row("INF000A01011"), row("INF000A01029")])
    write_fund_holdings(db, 7, [row("INF000A01011")])
    assert [r.isin for r in get_fund_holdings(db, 7, date(2025, 1, 31))] == ["INF000A01011"]
