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


def test_two_sources_land_in_one_transaction(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_store import get_statement_rows, write_fundamentals

    good, other = srow(), srow(item="net_income", value="5")
    write_fundamentals(db, 1, [good], "tapetide", more=[("tapetide_est", [other])])  # type: ignore[list-item]
    assert db.execute("select item, source from fundamental order by item").fetchall() == [
        ("net_income", "tapetide_est"), ("revenue", "tapetide"),
    ]  # fmt: skip
    overflow = srow(item="eps", value="1" + "0" * 30)  # does not fit DECIMAL(28,4)
    with pytest.raises(duckdb.Error):
        write_fundamentals(
            db, 1, [srow(item="cfo")], "tapetide", more=[("tapetide_est", [overflow])]
        )  # type: ignore[list-item]
    assert [r.item for r in get_statement_rows(db, 1)] == ["net_income", "revenue"]  # all or none


def test_reported_row_replaces_the_estimated_row_of_the_same_period(
    db: duckdb.DuckDBPyConnection,
) -> None:
    from nivesh_core.market_store import get_statement_rows, write_fundamentals

    est = srow(filed=date(2026, 5, 31))
    write_fundamentals(db, 1, [est, srow(item="cfo")], "tapetide_est")  # type: ignore[list-item]
    write_fundamentals(db, 1, [srow(value="12", filed=date(2026, 4, 27))], "tapetide",  # type: ignore[list-item]
                       supersede="tapetide_est")  # fmt: skip
    got = {(r.item, r.value, r.filed_at) for r in get_statement_rows(db, 1)}
    assert got == {("revenue", D("12"), date(2026, 4, 27)), ("cfo", D("10"), DAY)}


def test_shareholding_can_carry_its_own_source(db: duckdb.DuckDBPyConnection) -> None:
    from nivesh_core.market_models import ShareholdingRow
    from nivesh_core.market_store import write_fundamentals

    row = ShareholdingRow(period_end=DAY, promoter_pct=D("50"), filed_at=DAY)
    write_fundamentals(
        db, 1, [], "tapetide", shareholding=[row], shareholding_source="tapetide_est"
    )
    assert db.execute("select source from shareholding").fetchall() == [("tapetide_est",)]


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


# --- #131 source-aware reads ---------------------------------------------------------------------

import ast  # noqa: E402

from nivesh_core.market_models import ShareholdingRow  # noqa: E402
from nivesh_core.market_store import (  # noqa: E402
    get_shareholding,
    get_shareholding_by_source,
    get_shareholding_many,
    get_statement_rows,
    get_statement_rows_by_source,
    get_statement_rows_many,
    write_fundamentals,
)
from nivesh_engine.statements import StatementRow, latest_as_of  # noqa: E402

READERS = ("get_statement_rows", "get_statement_rows_many", "get_shareholding",
           "get_shareholding_many")  # fmt: skip
FY = date(2025, 3, 31)
Q4 = date(2025, 3, 31)


def srec(item: str, value: str, filed: str = "2025-06-01", ptype: str = "A") -> StatementRow:
    return StatementRow(period_end=FY, period_type=ptype, item=item, value=D(value),  # type: ignore[arg-type]
                        currency="INR", filed_at=date.fromisoformat(filed))  # fmt: skip


def put(db: duckdb.DuckDBPyConnection, source: str, *rows: StatementRow, sid: int = 1) -> None:
    write_fundamentals(db, sid, list(rows), source)


def held(
    promoter: str | None, filed: str = "2025-05-01", *, pledged: str | None = None,
    public: str | None = None, period: date = Q4,
) -> ShareholdingRow:  # fmt: skip
    def num(v: str | None) -> Decimal | None:
        return None if v is None else D(v)

    return ShareholdingRow(period_end=period, promoter_pct=num(promoter),
                           promoter_pledged_pct=num(pledged), public_pct=num(public),
                           filed_at=date.fromisoformat(filed))  # fmt: skip


def values(rows: list[StatementRow]) -> list[tuple[str, Decimal, str]]:
    return [(r.item, r.value, r.filed_at.isoformat()) for r in rows]


@pytest.fixture
def two(db: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    put(db, "tapetide", srec("net_income", "100"), srec("revenue", "500"))
    put(db, "upstox", srec("net_income", "80"), srec("cfo", "70"))
    return db


def test_the_first_listed_source_wins_each_figure(two: duckdb.DuckDBPyConnection) -> None:
    ranked = get_statement_rows(two, 1, sources=("tapetide", "upstox"))
    assert values(ranked) == [
        ("cfo", D("70"), "2025-06-01"),
        ("net_income", D("100"), "2025-06-01"),
        ("revenue", D("500"), "2025-06-01"),
    ]
    flipped = get_statement_rows(two, 1, sources=("upstox", "tapetide"))
    assert values(flipped) == [
        ("cfo", D("70"), "2025-06-01"),
        ("net_income", D("80"), "2025-06-01"),
        ("revenue", D("500"), "2025-06-01"),
    ]


def test_no_sources_is_the_legacy_every_row_read(two: duckdb.DuckDBPyConnection) -> None:
    assert len(get_statement_rows(two, 1)) == 4
    assert len(get_statement_rows(two, 1, sources=None)) == 4
    assert len(get_statement_rows(two, 1, sources=())) == 4  # nothing to rank by


def test_a_figure_only_the_lower_priority_source_has_still_appears(
    two: duckdb.DuckDBPyConnection,
) -> None:
    items = {r.item for r in get_statement_rows(two, 1, sources=("tapetide", "upstox"))}
    assert items == {"net_income", "revenue", "cfo"}  # cfo is upstox only


def test_an_unlisted_source_ranks_last_and_ties_resolve_by_name(
    db: duckdb.DuckDBPyConnection,
) -> None:
    put(db, "zeta", srec("revenue", "3"))
    put(db, "alpha", srec("revenue", "2"))
    put(db, "tapetide", srec("revenue", "1"))
    assert [r.value for r in get_statement_rows(db, 1, sources=("tapetide",))] == [D("1")]
    # none of the three is listed: one of them wins, by name, the same one every time
    assert [r.value for r in get_statement_rows(db, 1, sources=("nobody",))] == [D("2")]
    assert [r.value for r in get_statement_rows(db, 1, sources=("zeta",))] == [D("3")]


def test_source_names_are_bound_parameters_never_sql(two: duckdb.DuckDBPyConnection) -> None:
    evil = "x' OR 1=1) --"
    plain = get_statement_rows(two, 1, sources=("tapetide", "upstox"))
    assert get_statement_rows(two, 1, sources=(evil, "tapetide", "upstox")) == plain
    assert get_shareholding(two, 1, sources=(evil,)) == []


def test_one_source_returns_exactly_what_the_legacy_read_did(
    db: duckdb.DuckDBPyConnection,
) -> None:
    put(db, "tapetide", srec("revenue", "5"), srec("revenue", "6", "2025-09-01"),
        srec("eps", "2", ptype="Q"))  # fmt: skip
    put(db, "tapetide", srec("revenue", "9"), sid=2)
    legacy = get_statement_rows(db, 1)
    for listed in (("tapetide",), ("tapetide", "upstox"), ("upstox", "tapetide")):
        assert get_statement_rows(db, 1, sources=listed) == legacy
    assert get_statement_rows_many(db, [1, 2, 3], sources=("tapetide",)) == (
        get_statement_rows_many(db, [1, 2, 3])
    )
    write_fundamentals(db, 1, [], "tapetide", shareholding=[held("50"), held("51", "2025-08-01")])
    assert get_shareholding(db, 1, sources=("tapetide",)) == get_shareholding(db, 1)


def test_every_filed_version_of_the_winning_source_is_kept(
    db: duckdb.DuckDBPyConnection,
) -> None:
    put(db, "tapetide", srec("net_income", "100"), srec("net_income", "104", "2025-09-01"))
    put(db, "upstox", srec("net_income", "80", "2025-07-01"))
    rows = get_statement_rows(db, 1, sources=("tapetide", "upstox"))
    assert values(rows) == [("net_income", D("100"), "2025-06-01"),
                            ("net_income", D("104"), "2025-09-01")]  # fmt: skip
    assert [r.value for r in latest_as_of(rows)] == [D("104")]  # the restatement still wins
    assert [r.value for r in latest_as_of(rows, date(2025, 7, 15))] == [D("100")]


def test_a_priority_row_filed_after_as_of_does_not_hide_the_row_that_existed(
    db: duckdb.DuckDBPyConnection,
) -> None:
    put(db, "upstox", srec("net_income", "80", "2025-06-01"))
    put(db, "tapetide", srec("net_income", "100", "2025-11-01"))  # priority source, filed later
    order = ("tapetide", "upstox")
    before = date(2025, 8, 1)
    assert values(get_statement_rows(db, 1, before, sources=order)) == [
        ("net_income", D("80"), "2025-06-01")
    ]
    assert values(get_statement_rows_many(db, [1], before, sources=order)[1]) == [
        ("net_income", D("80"), "2025-06-01")
    ]
    after = get_statement_rows(db, 1, date(2025, 12, 1), sources=order)
    assert values(after) == [("net_income", D("100"), "2025-11-01")]
    assert values(get_statement_rows(db, 1, before)) == [("net_income", D("80"), "2025-06-01")]
    assert get_statement_rows(db, 1, date(2025, 1, 1), sources=order) == []


def test_many_ranks_each_security_on_its_own(db: duckdb.DuckDBPyConnection) -> None:
    put(db, "tapetide", srec("net_income", "1"), sid=1)
    put(db, "upstox", srec("net_income", "2"), sid=1)
    put(db, "upstox", srec("net_income", "3"), sid=2)  # only the low source has this security
    got = get_statement_rows_many(db, [1, 2, 9], sources=("tapetide", "upstox"))
    assert {k: [r.value for r in v] for k, v in got.items()} == {
        1: [D("1")],
        2: [D("3")],
        9: [],
    }


def test_shareholding_takes_one_whole_row_per_quarter_by_priority(
    db: duckdb.DuckDBPyConnection,
) -> None:
    write_fundamentals(db, 1, [], "bse_xbrl", shareholding=[held("40", pledged="9", public="60")])
    write_fundamentals(db, 1, [], "tapetide", shareholding=[held("41", "2025-05-02", public="59")])
    got = get_shareholding(db, 1, sources=("tapetide", "bse_xbrl"))
    assert [(g.promoter_pct, g.promoter_pledged_pct, g.public_pct) for g in got] == [
        (D("41"), None, D("59"))  # the pledge is NOT filled in from the other provider's filing
    ]
    swapped = get_shareholding(db, 1, sources=("bse_xbrl", "tapetide"))
    assert [(g.promoter_pct, g.promoter_pledged_pct) for g in swapped] == [(D("40"), D("9"))]
    legacy = get_shareholding(db, 1)  # no ranking: the latest filing, whoever filed it
    assert [g.promoter_pct for g in legacy] == [D("41")]


def test_shareholding_priority_then_latest_filing_and_point_in_time(
    db: duckdb.DuckDBPyConnection,
) -> None:
    write_fundamentals(db, 1, [], "tapetide", shareholding=[held("50", "2025-05-01"),
                                                           held("52", "2025-09-01")])  # fmt: skip
    write_fundamentals(db, 1, [], "bse_xbrl", shareholding=[held("48", "2025-05-05"),
                                                           held("49", "2025-12-01")])  # fmt: skip
    order = ("tapetide", "bse_xbrl")
    assert [g.promoter_pct for g in get_shareholding(db, 1, sources=order)] == [D("52")]
    mid = date(2025, 7, 1)
    assert [g.promoter_pct for g in get_shareholding(db, 1, mid, sources=order)] == [D("50")]
    assert [g.promoter_pct for g in get_shareholding_many(db, [1], mid, sources=order)[1]] == [
        D("50")
    ]
    early = date(2025, 5, 3)  # only tapetide's first filing existed
    assert [g.promoter_pct for g in get_shareholding(db, 1, early, sources=order)] == [D("50")]
    only_low = date(2025, 5, 4)
    write_fundamentals(db, 2, [], "bse_xbrl", shareholding=[held("30", "2025-05-04")])
    write_fundamentals(db, 2, [], "tapetide", shareholding=[held("31", "2025-08-01")])
    got = get_shareholding_many(db, [2], only_low, sources=order)[2]
    assert [g.promoter_pct for g in got] == [D("30")]  # a later priority filing never hides it


def test_by_source_reads_every_source_apart_and_respects_as_of(
    two: duckdb.DuckDBPyConnection,
) -> None:
    put(two, "tapetide", srec("net_income", "101", "2025-10-01"))
    by = get_statement_rows_by_source(two, 1)
    assert sorted(by) == ["tapetide", "upstox"]
    assert [(r.item, r.value) for r in by["upstox"]] == [("cfo", D("70")), ("net_income", D("80"))]
    assert len(by["tapetide"]) == 3  # all filed versions
    early = get_statement_rows_by_source(two, 1, date(2025, 7, 1))
    assert len(early["tapetide"]) == 2
    assert get_statement_rows_by_source(two, 7) == {}
    write_fundamentals(two, 1, [], "tapetide", shareholding=[held("50"), held("51", "2025-08-01")])
    write_fundamentals(two, 1, [], "bse_xbrl", shareholding=[held("49", "2025-06-01")])
    sh = get_shareholding_by_source(two, 1)
    assert {k: [g.promoter_pct for g in v] for k, v in sh.items()} == {
        "tapetide": [D("51")],
        "bse_xbrl": [D("49")],
    }
    mid = get_shareholding_by_source(two, 1, date(2025, 7, 1))
    assert {k: [g.promoter_pct for g in v] for k, v in mid.items()} == {
        "tapetide": [D("50")],
        "bse_xbrl": [D("49")],
    }
    assert get_shareholding_by_source(two, 7) == {}


# the completeness test: no reader call site may forget its `sources=` --------------------------

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = sorted(d.name for d in ROOT.glob("nivesh_*") if d.is_dir())  # all of them


def _reader_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(local names bound to a reader, local names bound to the market_store module)."""
    funcs: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nivesh_core.market_store":
            funcs |= {a.asname or a.name for a in node.names if a.name in READERS}
        elif isinstance(node, ast.ImportFrom) and node.module == "nivesh_core":
            modules |= {a.asname or a.name for a in node.names if a.name == "market_store"}
        elif isinstance(node, ast.Import):
            modules |= {
                a.asname or a.name for a in node.names if a.name == "nivesh_core.market_store"
            }
    return funcs, modules


def reader_calls_without_sources(source: str) -> list[int]:
    """Line numbers of calls to the four readers (under any import alias) with no `sources=`."""
    tree = ast.parse(source)
    funcs, modules = _reader_names(tree)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        named = (isinstance(f, ast.Name) and f.id in funcs) or (
            isinstance(f, ast.Attribute)
            and f.attr in READERS
            and isinstance(f.value, ast.Name)
            and f.value.id in modules
        )
        if named and not any(k.arg == "sources" for k in node.keywords):
            bad.append(node.lineno)
    return bad


def test_every_reader_call_site_carries_a_sources_keyword() -> None:
    found: dict[str, list[int]] = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            if path.name == "market_store.py":
                continue
            bad = reader_calls_without_sources(path.read_text())
            if bad:
                found[str(path.relative_to(ROOT))] = bad
    assert found == {}, "a reader call without `sources=` ignores the configured provider order"


def test_the_completeness_scan_sees_aliases_and_module_calls() -> None:
    src = (
        "from nivesh_core.market_store import get_shareholding as held, get_bars\n"
        "from nivesh_core import market_store as ms\n"
        "import nivesh_core.market_store as whole\n"
        "def get_shareholding(): ...\n"
        "held(d, 1)\n"  # line 5: alias without sources
        "held(d, 1, sources=None)\n"
        "ms.get_statement_rows_many(d, [1])\n"  # line 7
        "whole.get_shareholding_many(d, [1], sources=('a',))\n"
        "get_shareholding()\n"  # the MCP tool of the same name: not a reader
        "get_bars(d, 1)\n"
    )
    assert reader_calls_without_sources(src) == [5, 7]
