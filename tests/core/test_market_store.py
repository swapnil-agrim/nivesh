from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.market_models import CorpAction, PriceBar
from nivesh_core.market_store import (
    bars_by_source,
    get_bars,
    get_corp_actions,
    save_corp_actions,
    upsert_bars,
)

D = Decimal
DAY = date(2026, 1, 5)


@pytest.fixture
def db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, MIGRATIONS / "duck")
    return c


def bar(source: str = "nse_bhavcopy", close: str = "10", day: date = DAY, **kw: object) -> PriceBar:
    return PriceBar(security_id=1, date=day, close=D(close), source=source, **kw)  # type: ignore[arg-type]


def test_upsert_bars_is_idempotent_per_source(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [bar(), bar("yahoo")])
    upsert_bars(db, [bar(close="11")])
    assert db.execute("select count(*) from price_bar").fetchone() == (2,)
    assert get_bars(db, 1)[0].close == D("11")


def test_get_bars_prefers_primary_source_and_exposes_flag(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [bar("yahoo", "9"), bar("nse_bhavcopy", "10", flag="mismatch")])
    upsert_bars(db, [bar("yahoo", "9", day=date(2026, 1, 6))])
    got = get_bars(db, 1)
    assert [(b.source, b.flag) for b in got] == [("nse_bhavcopy", "mismatch"), ("yahoo", None)]
    assert set(bars_by_source(db, 1)) == {"yahoo", "nse_bhavcopy"}
    assert get_bars(db, 1, DAY, DAY)[0].date == DAY and get_bars(db, 2) == []


def test_bars_roundtrip_decimal_exact(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [bar(close="1234.567891", open="1.000001", volume=5, adj_close="1234.567891")])
    b = get_bars(db, 1)[0]
    assert (b.close, b.open, b.volume, b.adj_close) == (
        D("1234.567891"), D("1.000001"), 5, D("1234.567891"),
    )  # fmt: skip


def test_upsert_is_atomic_on_a_bad_row(db: duckdb.DuckDBPyConnection) -> None:
    bad = PriceBar(
        security_id=1, date=DAY, close=D("10") ** 20, source="x"
    )  # overflows DECIMAL(18,6)
    with pytest.raises(duckdb.Error):
        upsert_bars(db, [bar("good"), bad])
    assert db.execute("select count(*) from price_bar").fetchone() == (0,)


def test_save_corp_actions_idempotent(db: duckdb.DuckDBPyConnection) -> None:
    a = CorpAction(
        security_id=1, ex_date=DAY, kind="split", ratio=D("4"), source="nse_corp_actions"
    )
    save_corp_actions(db, [a])
    save_corp_actions(db, [a, a.model_copy(update={"source": "yahoo"})])
    got = get_corp_actions(db, 1)
    assert [x.source for x in got] == ["nse_corp_actions", "yahoo"]
    assert get_corp_actions(db, 1, since=date(2026, 2, 1)) == []


# fundamentals, shareholding, filings -----------------------------------------------------------


def srow(item: str = "revenue", value: str = "10", filed: date = DAY, ptype: str = "A") -> object:
    from nivesh_engine.statements import StatementRow

    return StatementRow(period_end=date(2025, 12, 31), period_type=ptype, item=item,  # type: ignore[arg-type]
                        value=D(value), currency="USD", filed_at=filed)  # fmt: skip


def test_fundamentals_append_only_across_filed_at_and_idempotent(
    db: duckdb.DuckDBPyConnection,
) -> None:
    from nivesh_core.market_store import get_statement_rows, write_fundamentals

    write_fundamentals(db, 1, [srow(), srow(value="11", filed=date(2026, 3, 1))], "sec_edgar")  # type: ignore[list-item]
    write_fundamentals(db, 1, [srow()], "sec_edgar")  # type: ignore[list-item]
    rows = get_statement_rows(db, 1)
    assert [(r.value, r.filed_at) for r in rows] == [(D("10"), DAY), (D("11"), date(2026, 3, 1))]
    assert get_statement_rows(db, 2) == []


def test_shareholding_roundtrip_latest_filing_per_quarter(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_models import ShareholdingRow
    from nivesh_core.market_store import get_shareholding, write_fundamentals

    q = date(2025, 12, 31)
    old = ShareholdingRow(period_end=q, promoter_pct=D("50.1"), filed_at=DAY)
    new = ShareholdingRow(period_end=q, promoter_pct=D("50.2"), filed_at=date(2026, 2, 1))
    write_fundamentals(db, 1, [], "bse_xbrl", shareholding=[old, new])
    got = get_shareholding(db, 1)
    assert [g.promoter_pct for g in got] == [D("50.2000")]


def test_save_filing_is_idempotent_and_keeps_sections(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_store import (
        get_filing_sections,
        list_filings,
        save_filing,
    )

    kw = dict(security_id=1, form="10-K", filed_at=DAY, period_end=None, source="sec_edgar",
              url="https://example.invalid/d", doc_key="acc-1")  # fmt: skip
    a = save_filing(db, sections=None, **kw)  # type: ignore[arg-type]
    b = save_filing(db, sections={"business": "text", "mdna": "more"}, **kw)  # type: ignore[arg-type]
    assert a == b
    assert get_filing_sections(db, a) == {"business": "text", "mdna": "more"}
    other = dict(kw, form="8-K", doc_key="acc-2", filed_at=date(2026, 2, 1))
    save_filing(db, sections=None, **other)  # type: ignore[arg-type]
    rows = list_filings(db, 1)
    assert [r.form for r in rows] == ["8-K", "10-K"]  # newest first
    assert [r.form for r in list_filings(db, 1, forms=("10-K",))] == ["10-K"]
    assert [r.form for r in list_filings(db, 1, since=date(2026, 1, 15))] == ["8-K"]


# macro, estimates, events ----------------------------------------------------------------------


def test_macro_series_idempotent_and_range_read(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_store import get_macro, write_macro

    rows = [(date(2026, 1, 2), D("4.5")), (date(2026, 1, 6), D("-0.1"))]
    write_macro(db, "y10_us", rows, "fred")
    write_macro(db, "y10_us", [(date(2026, 1, 2), D("4.6"))], "fred")
    assert get_macro(db, "y10_us") == [(date(2026, 1, 2), D("4.6")), (date(2026, 1, 6), D("-0.1"))]
    assert get_macro(db, "y10_us", start=date(2026, 1, 3)) == [(date(2026, 1, 6), D("-0.1"))]
    assert get_macro(db, "y10_us", end=date(2026, 1, 2)) == [(date(2026, 1, 2), D("4.6"))]
    assert get_macro(db, "nope") == []


def test_estimates_snapshots_per_as_of_and_history(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_store import estimate_history, latest_estimates, write_estimates

    write_estimates(db, 1, [("eps", "2026-09-30", D("7.5")), ("revenue", "2026-09-30", D("100"))],
                    date(2026, 1, 5), "fmp")  # fmt: skip
    write_estimates(db, 1, [("eps", "2026-09-30", D("7.6"))], date(2026, 1, 5), "fmp")  # same day
    write_estimates(db, 1, [("eps", "2026-09-30", D("7.9"))], date(2026, 2, 5), "fmp")
    assert estimate_history(db, 1, "eps", "2026-09-30") == [
        (date(2026, 1, 5), D("7.6")),
        (date(2026, 2, 5), D("7.9")),
    ]
    latest = latest_estimates(db, 1)
    assert {(e.metric, e.value) for e in latest} == {("eps", D("7.9")), ("revenue", D("100"))}


def test_calendar_events_idempotent_and_windowed(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_store import get_events, write_events

    ev = [(1, "results", date(2026, 2, 10)), (2, "results", date(2026, 3, 1))]
    write_events(db, ev, "fmp", date(2026, 1, 5))
    write_events(db, ev, "fmp", date(2026, 1, 6))
    got = get_events(db, start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert [(g.security_id, g.event_date) for g in got] == [(1, date(2026, 2, 10))]
    assert len(get_events(db)) == 2
    assert [g.event_date for g in get_events(db, security_id=2)] == [date(2026, 3, 1)]
