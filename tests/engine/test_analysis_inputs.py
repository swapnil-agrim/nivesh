import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from nivesh_adapters.analysis_data import (
    load_auditor_filings,
    load_closes,
    load_flag_inputs,
    load_peer_multiples,
    load_valuation_inputs,
)
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_models import CorpAction, PriceBar
from nivesh_core.market_store import (
    filing_items_many,
    save_corp_actions,
    save_filing,
    upsert_bars,
    write_fundamentals,
)
from nivesh_core.security_master import build_master
from nivesh_engine.redflags import EightK, detect_flags
from nivesh_engine.valuation import MULTIPLES, current_multiples, valuation_multiples
from tests.analysis_fx import annual_rows, share_rows, strow
from tests.market_fx import mrow

D = Decimal
CFG = AnalysisSettings()


@pytest.fixture
def db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, MIGRATIONS / "duck")
    return c


@pytest.fixture
def master(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, int]]:
    init_stores(tmp_path / "d")
    conn = open_sqlite(tmp_path / "d" / "nivesh.sqlite")
    build_master(conn, [
        mrow("AAA", industry="Widgets", sector="Industrials"),
        mrow("BBB", industry="Widgets"),
        mrow("DDD", industry="Widgets"),
        mrow("EEE", industry="Widgets"),
        mrow("BNK", industry="Banks", sector="Banks"),
        mrow("USCO", "NASDAQ", market="US", currency="USD"),
    ], [])  # fmt: skip
    ids = {r[0]: r[1] for r in conn.execute("select symbol, id from security")}
    return conn, ids


def bar(sid: int, d: str, close: str, source: str = "nse_bhavcopy") -> PriceBar:
    return PriceBar(security_id=sid, date=date.fromisoformat(d), close=D(close), source=source)


def put(db: duckdb.DuckDBPyConnection, sid: int, rows: list) -> None:  # type: ignore[type-arg]
    for r in rows:
        write_fundamentals(db, sid, [r], "bse_xbrl")


# ---- month-end closes ---------------------------------------------------------------------------
def test_closes_are_raw_month_ends_and_exclude_the_partial_as_of_month(
    db: duckdb.DuckDBPyConnection,
) -> None:
    upsert_bars(db, [bar(1, "2024-01-31", "100"), bar(1, "2024-02-15", "90"),
                     bar(1, "2024-02-28", "110"), bar(1, "2024-03-05", "120"),
                     bar(1, "2024-03-10", "130")])  # fmt: skip
    save_corp_actions(db, [CorpAction(security_id=1, ex_date=date(2024, 2, 20), kind="split",
                                      ratio=D(2), source="nse")])  # fmt: skip
    got = load_closes(db, [1, 2], date(2024, 3, 10), years=1)
    assert got[1].month_ends == ((date(2024, 1, 31), D(100)), (date(2024, 2, 28), D(110)))
    assert got[1].last == (date(2024, 3, 10), D(130))  # raw close: a split is not applied
    assert got[2].month_ends == () and got[2].last is None


def test_closes_include_the_as_of_month_when_as_of_is_a_month_end(
    db: duckdb.DuckDBPyConnection,
) -> None:
    upsert_bars(db, [bar(1, "2024-02-28", "110"), bar(1, "2024-03-28", "120"),
                     bar(1, "2024-04-02", "125")])  # fmt: skip
    got = load_closes(db, [1], date(2024, 3, 31), years=1)
    assert got[1].month_ends[-1] == (date(2024, 3, 28), D(120))
    assert got[1].last == (date(2024, 3, 28), D(120))  # nothing after as_of leaks in


def test_closes_window_follows_the_requested_years(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [bar(1, "2020-01-31", "50"), bar(1, "2023-12-29", "100"),
                     bar(1, "2024-03-28", "120")])  # fmt: skip
    near = load_closes(db, [1], date(2024, 3, 31), years=1)
    assert [d for d, _ in near[1].month_ends] == [date(2023, 12, 29), date(2024, 3, 28)]
    none = load_closes(db, [1], date(2024, 3, 31), years=0)
    assert none[1].month_ends == () and none[1].last == (date(2024, 3, 28), D(120))


# ---- valuation inputs and peers -----------------------------------------------------------------
def test_valuation_inputs_use_only_rows_filed_by_as_of_and_label_market_and_kind(
    db: duckdb.DuckDBPyConnection, master: tuple[sqlite3.Connection, dict[str, int]]
) -> None:
    sql, ids = master
    rows = annual_rows({"eps": [8, 10]}, currency="INR")  # FY2023 filed 2024-02-15
    put(db, ids["AAA"], rows)
    put(db, ids["BNK"], rows)
    upsert_bars(db, [bar(ids["AAA"], "2024-03-28", "80")])
    got = load_valuation_inputs(db, sql, [ids["AAA"], ids["BNK"], 99999], date(2023, 12, 31), CFG)
    assert set(got) == {ids["AAA"], ids["BNK"]}
    a = got[ids["AAA"]]
    assert a.market == "IN" and a.sector_kind == "general" and a.as_of == date(2023, 12, 31)
    assert {r.period_end.year for r in a.rows} == {2022} and a.last_close is None
    assert got[ids["BNK"]].sector_kind == "financial"
    late = load_valuation_inputs(db, sql, [ids["AAA"]], date(2024, 3, 31), CFG)[ids["AAA"]]
    assert {r.period_end.year for r in late.rows} == {2022, 2023}
    assert late.last_close == (date(2024, 3, 28), D(80))
    result = valuation_multiples(late, None, cfg=CFG)
    assert result.multiples["pe"].current.value == D(8)


def test_peer_multiples_collect_each_peers_current_values_sorted_by_symbol(
    db: duckdb.DuckDBPyConnection, master: tuple[sqlite3.Connection, dict[str, int]]
) -> None:
    sql, ids = master
    put(db, ids["BBB"], annual_rows({"eps": [5]}, currency="INR"))
    put(db, ids["DDD"], annual_rows({"eps": [4]}, currency="INR"))
    put(db, ids["EEE"], [strow("eps", "-1", date(2023, 12, 31), date(2024, 2, 15), currency="INR")])
    upsert_bars(db, [bar(ids["BBB"], "2024-03-28", "50"), bar(ids["DDD"], "2024-03-28", "60"),
                     bar(ids["EEE"], "2024-03-28", "70")])  # fmt: skip
    peers = load_peer_multiples(db, sql, ids["AAA"], date(2024, 3, 31), CFG)
    assert peers.symbols == ("BBB", "DDD", "EEE") and peers.considered == 3
    assert peers.values["pe"] == (D(10), D(15))  # EEE has a loss: no meaningful P/E
    assert all(peers.values[m] == () for m in MULTIPLES if m != "pe")
    assert peers.reason is None


def test_peer_multiples_without_an_industry_say_why(
    db: duckdb.DuckDBPyConnection, master: tuple[sqlite3.Connection, dict[str, int]]
) -> None:
    sql, ids = master
    sql.execute("update security set industry = null where id = ?", (ids["AAA"],))
    peers = load_peer_multiples(db, sql, ids["AAA"], date(2024, 3, 31), CFG)
    assert peers.considered == 0 and peers.values["pe"] == () and "industry" in (peers.reason or "")
    inputs = load_valuation_inputs(db, sql, [ids["AAA"]], date(2024, 3, 31), CFG)[ids["AAA"]]
    assert current_multiples(inputs)["pe"].available is False


# ---- 8-K Item 4.01 filings and flag inputs ------------------------------------------------------
def filing(
    db: duckdb.DuckDBPyConnection,
    sid: int,
    form: str,
    filed: str,
    sections: dict[str, str] | None,
    key: str,
) -> int:
    return save_filing(db, security_id=sid, form=form, filed_at=date.fromisoformat(filed),
                       period_end=None, source="sec_edgar", url=None, doc_key=key,
                       sections=sections)  # fmt: skip


def seed_filings(db: duckdb.DuckDBPyConnection, a: int, b: int) -> dict[str, int]:
    note = "Changes in the registrant's certifying accountant."
    return {
        "change": filing(db, a, "8-K", "2024-01-10", {"item_4.01": note, "item_9.01": "x"}, "k1"),
        "other": filing(db, a, "8-K", "2024-03-01", {"item_2.02": "results"}, "k2"),
        "amended": filing(db, a, "8-K/A", "2024-04-01", {"item_4.01": note}, "k3"),
        "annual": filing(db, a, "10-K", "2024-02-01", {"item_4.01": note}, "k4"),
        "no_text": filing(db, a, "8-K", "2024-05-01", None, "k5"),
        "late": filing(db, a, "8-K", "2024-09-01", {"item_4.01": note}, "k6"),
        "old": filing(db, a, "8-K", "2021-01-01", {"item_4.01": note}, "k7"),
        "other_sec": filing(db, b, "8-K", "2024-02-02", {"item_5.02": "officers"}, "k8"),
    }


def test_filing_items_many_lists_eight_ks_with_sections_in_the_window(
    db: duckdb.DuckDBPyConnection,
) -> None:
    made = seed_filings(db, 1, 2)
    got = filing_items_many(db, [1, 2, 3], ["8-K"], date(2022, 6, 30), date(2024, 6, 30))
    assert [f.filing_id for f in got[1]] == [made["amended"], made["other"], made["change"]]
    assert got[1][2].sections == ("item_4.01", "item_9.01") and got[1][2].filed_at == date(
        2024, 1, 10
    )
    assert [f.sections for f in got[2]] == [("item_5.02",)] and got[3] == []


def test_filing_items_many_uses_one_statement_for_many_securities(
    db: duckdb.DuckDBPyConnection,
) -> None:
    seed_filings(db, 1, 2)
    calls: list[str] = []

    class Spy:
        def execute(self, *a: object, **k: object) -> object:
            calls.append("q")
            return db.execute(*a, **k)  # type: ignore[arg-type]

    filing_items_many(Spy(), list(range(1, 40)), ["8-K"], date(2022, 6, 30), date(2024, 6, 30))  # type: ignore[arg-type]
    assert len(calls) == 1


def test_auditor_filings_map_item_4_01_and_ignore_other_forms_and_dates(
    db: duckdb.DuckDBPyConnection,
) -> None:
    made = seed_filings(db, 1, 2)
    got = load_auditor_filings(db, [1, 2, 3], date(2024, 6, 30), CFG)
    assert got[1] == (
        EightK(made["amended"], date(2024, 4, 1), True),
        EightK(made["other"], date(2024, 3, 1), False),
        EightK(made["change"], date(2024, 1, 10), True),
    )
    assert got[2] == (EightK(made["other_sec"], date(2024, 2, 2), False),) and got[3] == ()


def test_flag_inputs_assemble_market_rows_pledge_and_filings(
    db: duckdb.DuckDBPyConnection, master: tuple[sqlite3.Connection, dict[str, int]]
) -> None:
    sql, ids = master
    seed_filings(db, ids["USCO"], ids["AAA"])
    put(db, ids["USCO"], annual_rows({"cfo": [40] * 3, "net_income": [100] * 3}))
    put(db, ids["AAA"], annual_rows({"net_income": [100]}, currency="INR"))
    write_fundamentals(db, ids["AAA"], [], "bse_shareholding", shareholding=share_rows(
        [("50", "18"), ("50", "21"), ("50", "25")]))  # fmt: skip
    asof = date(2024, 6, 30)
    us = load_flag_inputs(db, sql, ids["USCO"], asof, CFG, contingent_liabilities=D(5))
    assert us is not None and us.market == "US" and us.auditor is not None
    assert us.contingent_liabilities == D(5) and len(us.rows) == 6
    flags = {f.flag: f for f in detect_flags(us, as_of=asof, cfg=CFG)}
    assert flags["auditor_change"].status == "fired" and flags["cfo_to_pat"].severity == "hard"
    india = load_flag_inputs(db, sql, ids["AAA"], asof, CFG)
    assert india is not None and india.market == "IN" and india.auditor is None
    assert len(india.shareholding) == 3
    assert {f.flag: f for f in detect_flags(india, as_of=asof, cfg=CFG)}[
        "pledge"
    ].severity == "hard"
    assert load_flag_inputs(db, sql, 99999, asof, CFG) is None
    early = load_flag_inputs(db, sql, ids["AAA"], date(2023, 5, 1), CFG)
    assert early is not None and len(early.shareholding) == 1 and early.rows == ()
