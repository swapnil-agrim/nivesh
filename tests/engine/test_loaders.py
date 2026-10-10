import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

import nivesh_mcp.common as common
from nivesh_adapters.analysis_data import (
    benchmark_for,
    load_bars,
    load_estimates,
    load_peers,
    load_shareholding,
    load_statements,
    sector_index_for,
    security_meta,
)
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_models import CorpAction, PriceBar, ShareholdingRow
from nivesh_core.market_store import (
    get_bars,
    get_bars_many,
    get_corp_actions,
    get_corp_actions_many,
    get_shareholding,
    get_shareholding_many,
    get_statement_rows,
    get_statement_rows_many,
    latest_estimates,
    latest_estimates_many,
    save_corp_actions,
    upsert_bars,
    write_estimates,
    write_fundamentals,
)
from nivesh_core.security_master import SecurityMaster, build_master
from nivesh_engine.adjust import adjust_closes, split_adjusted_map
from nivesh_engine.bars import Bar, to_bars
from nivesh_engine.statements import StatementRow
from tests.analysis_fx import day, pbar
from tests.market_fx import mrow
from tests.mcp.mkt_fx import NOW, Mkt, call, seed_master

D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, MIGRATIONS / "duck")
    return c


class Counting:
    """A connection proxy that counts statements (DuckDB's own class cannot be patched)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn, self.n = conn, 0

    def execute(self, *args: Any, **kw: Any) -> Any:
        self.n += 1
        return self.conn.execute(*args, **kw)


def srow(sid: int, item: str, value: str, end: str, filed: str) -> tuple[int, StatementRow]:
    e = date.fromisoformat(end)
    return sid, StatementRow(period_end=e, period_type="A", item=item, value=D(value),
                             currency="INR", filed_at=date.fromisoformat(filed))  # fmt: skip


def put_rows(db: duckdb.DuckDBPyConnection, *rows: tuple[int, StatementRow]) -> None:
    for sid, r in rows:
        write_fundamentals(db, sid, [r], "bse_xbrl")


# ---- bulk queries -------------------------------------------------------------------------------
def test_get_bars_many_matches_get_bars_per_security_including_best_source(
    db: duckdb.DuckDBPyConnection,
) -> None:
    upsert_bars(db, [pbar(1, 0, "10"), pbar(1, 0, "9", "yahoo"), pbar(1, 1, "11", "yahoo"),
                     pbar(2, 0, "20", "stooq"), pbar(2, 3, "21", "nse_bhavcopy")])  # fmt: skip
    got = get_bars_many(db, [1, 2, 3])
    assert got == {i: get_bars(db, i) for i in (1, 2, 3)}
    assert got[3] == [] and [b.source for b in got[1]] == ["nse_bhavcopy", "yahoo"]
    windowed = get_bars_many(db, [1, 2], day(1), day(2))
    assert windowed == {i: get_bars(db, i, day(1), day(2)) for i in (1, 2)}
    assert get_bars_many(db, []) == {}


def test_get_bars_many_issues_a_constant_number_of_queries_for_many_ids(
    db: duckdb.DuckDBPyConnection,
) -> None:
    ids = list(range(1, 51))
    upsert_bars(db, [pbar(i, k, str(10 + k)) for i in ids for k in range(3)])
    save_corp_actions(db, [CorpAction(security_id=i, ex_date=day(1), kind="split", ratio=D(2),
                                      source="nse") for i in ids])  # fmt: skip
    for i in ids:
        put_rows(db, srow(i, "revenue", "5", "2024-03-31", "2024-06-01"))
        held = ShareholdingRow(period_end=date(2024, 3, 31), promoter_pct=D(50),
                               filed_at=date(2024, 5, 1))  # fmt: skip
        write_fundamentals(db, i, [], "x", shareholding=[held])
        write_estimates(db, i, [("revenue", "FY25", D(9))], day(2), "fmp")
    spy = Counting(db)
    c: Any = spy
    assert len(get_bars_many(c, ids)) == 50 and spy.n <= 2
    spy.n = 0
    assert len(get_corp_actions_many(c, ids)) == 50 and spy.n <= 2
    spy.n = 0
    assert len(get_statement_rows_many(c, ids)) == 50 and spy.n <= 2
    spy.n = 0
    assert len(get_shareholding_many(c, ids)) == 50 and spy.n <= 2
    spy.n = 0
    assert len(latest_estimates_many(c, ids)) == 50 and spy.n <= 2


def test_ids_beyond_one_chunk_are_all_returned(db: duckdb.DuckDBPyConnection) -> None:
    ids = list(range(1, 1203))
    upsert_bars(db, [pbar(i, 0, "10") for i in ids])
    spy = Counting(db)
    got = get_bars_many(spy, ids)  # type: ignore[arg-type]
    assert len(got) == 1202 and all(len(v) == 1 for v in got.values()) and spy.n == 3


def test_corp_actions_many_matches_per_security(db: duckdb.DuckDBPyConnection) -> None:
    save_corp_actions(db, [
        CorpAction(security_id=1, ex_date=day(2), kind="split", ratio=D(2), source="yahoo"),
        CorpAction(security_id=1, ex_date=day(2), kind="bonus", ratio=D(1), source="nse"),
        CorpAction(security_id=2, ex_date=day(5), kind="dividend", amount=D(3), source="nse"),
    ])  # fmt: skip
    got = get_corp_actions_many(db, [1, 2, 3], day(1))
    assert got == {i: get_corp_actions(db, i, day(1)) for i in (1, 2, 3)} and got[3] == []


def test_statements_many_returns_every_filed_version_and_filters_filed_after_as_of(
    db: duckdb.DuckDBPyConnection,
) -> None:
    put_rows(
        db,
        srow(1, "revenue", "100", "2024-03-31", "2024-06-01"),
        srow(1, "revenue", "110", "2024-03-31", "2024-09-01"),  # restated later
        srow(2, "revenue", "50", "2024-03-31", "2024-06-15"),
    )
    everything = get_statement_rows_many(db, [1, 2, 3])
    assert everything == {i: get_statement_rows(db, i) for i in (1, 2, 3)}
    assert [r.value for r in everything[1]] == [D(100), D(110)] and everything[3] == []
    cut = get_statement_rows_many(db, [1, 2], date(2024, 6, 10))
    assert [r.value for r in cut[1]] == [D(100)] and cut[2] == []


def test_shareholding_many_one_row_per_quarter_and_filed_after_as_of_excluded(
    db: duckdb.DuckDBPyConnection,
) -> None:
    def sh(end: str, pledged: str, filed: str) -> ShareholdingRow:
        return ShareholdingRow(
            period_end=date.fromisoformat(end), promoter_pct=D(60),
            promoter_pledged_pct=D(pledged), filed_at=date.fromisoformat(filed),
        )  # fmt: skip

    write_fundamentals(db, 1, [], "x", shareholding=[
        sh("2024-03-31", "10", "2024-04-20"),
        sh("2024-03-31", "30", "2024-09-01"),  # restated after the as-of date
        sh("2024-06-30", "12", "2024-07-20"),
        sh("2024-09-30", "14", "2024-10-20"),  # filed after the as-of date
    ])  # fmt: skip
    write_fundamentals(db, 2, [], "x", shareholding=[sh("2024-03-31", "5", "2024-04-25")])
    assert get_shareholding_many(db, [1, 2, 3]) == {i: get_shareholding(db, i) for i in (1, 2, 3)}
    cut = get_shareholding_many(db, [1, 2], date(2024, 8, 1))
    # the restatement filed after as_of must not hide the filing that existed on as_of
    assert [(r.period_end, r.promoter_pledged_pct) for r in cut[1]] == [
        (date(2024, 3, 31), D(10)), (date(2024, 6, 30), D(12)),
    ]  # fmt: skip
    assert len(cut[2]) == 1


def test_estimates_many_latest_per_security(db: duckdb.DuckDBPyConnection) -> None:
    write_estimates(db, 1, [("revenue", "FY25", D(9)), ("eps", "FY25", D(2))], day(1), "fmp")
    write_estimates(db, 1, [("revenue", "FY25", D(10))], day(5), "fmp")
    write_estimates(db, 2, [("eps", "FY26", D(3))], day(2), "fmp")
    got = latest_estimates_many(db, [1, 2, 3])
    assert got == {i: latest_estimates(db, i) for i in (1, 2, 3)} and got[3] == []
    assert next(e.value for e in got[1] if e.metric == "revenue") == D(10)
    early = latest_estimates_many(db, [1], day(3))
    assert next(e.value for e in early[1] if e.metric == "revenue") == D(9)


# ---- adjusted basis -----------------------------------------------------------------------------
def test_loader_adjusts_closes_for_split_and_bonus_and_never_for_dividends(
    db: duckdb.DuckDBPyConnection,
) -> None:
    upsert_bars(db, [pbar(1, 0, "100", open="102", high="104", low="98", volume=1000),
                     pbar(1, 2, "50", volume=2000),
                     pbar(2, 0, "90"), pbar(2, 2, "60"),
                     pbar(3, 0, "100"), pbar(3, 2, "95")])  # fmt: skip
    save_corp_actions(db, [
        CorpAction(security_id=1, ex_date=day(1), kind="split", ratio=D(2), source="nse"),
        CorpAction(security_id=2, ex_date=day(1), kind="bonus", ratio=D(1), source="nse"),
        CorpAction(security_id=3, ex_date=day(1), kind="dividend", amount=D(5), source="nse"),
    ])  # fmt: skip
    got = load_bars(db, [1, 2, 3])
    assert [b.close for b in got[1]] == [D(50), D(50)]
    assert got[1][0] == Bar(day(0), D(51), D(52), D(49), D(50), D(2000))
    assert [b.close for b in got[2]] == [D(45), D(60)]  # 1:1 bonus halves the earlier close
    assert [b.close for b in got[3]] == [D(100), D(95)]  # a dividend is not a price adjustment


def test_loader_does_not_split_adjust_a_yahoo_source_twice(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [pbar(1, 0, "50", "yahoo"), pbar(1, 2, "50", "yahoo")])
    save_corp_actions(db, [CorpAction(security_id=1, ex_date=day(1), kind="split", ratio=D(2),
                                      source="yahoo")])  # fmt: skip
    assert [b.close for b in load_bars(db, [1])[1]] == [D(50), D(50)]


def test_loader_end_date_excludes_later_bars(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [pbar(1, i, str(10 + i)) for i in range(6)])
    got = load_bars(db, [1, 2], end=day(3))
    assert [b.date for b in got[1]] == [day(i) for i in range(4)] and got[2] == []
    from_start = load_bars(db, [1], start=day(2), end=day(3))
    assert [b.date for b in from_start[1]] == [day(2), day(3)]


def test_security_without_bars_gets_an_empty_list_not_an_error(
    db: duckdb.DuckDBPyConnection,
) -> None:
    assert load_bars(db, [7, 8]) == {7: [], 8: []}
    assert load_statements(db, [7]) == {7: []} and load_shareholding(db, [7]) == {7: []}
    assert load_estimates(db, [7]) == {7: []}


def test_loader_applies_only_actions_known_by_the_end_date(db: duckdb.DuckDBPyConnection) -> None:
    upsert_bars(db, [pbar(1, 0, "100"), pbar(1, 2, "100"), pbar(1, 4, "50")])
    save_corp_actions(db, [CorpAction(security_id=1, ex_date=day(3), kind="split", ratio=D(2),
                                      source="nse")])  # fmt: skip
    assert [b.close for b in load_bars(db, [1])[1]] == [D(50), D(50), D(50)]
    assert [b.close for b in load_bars(db, [1], end=day(2))[1]] == [D(100), D(100)]


def test_loader_statement_and_shareholding_wrappers_pass_as_of(
    db: duckdb.DuckDBPyConnection,
) -> None:
    put_rows(db, srow(1, "revenue", "100", "2024-03-31", "2024-06-01"),
             srow(1, "revenue", "110", "2024-03-31", "2024-09-01"))  # fmt: skip
    assert [r.value for r in load_statements(db, [1], as_of=date(2024, 7, 1))[1]] == [D(100)]
    assert len(load_statements(db, [1])[1]) == 2


def test_to_bars_scales_open_high_low_by_the_adjustment_factor_and_volume_inversely() -> None:
    src = PriceBar(security_id=1, date=day(0), open=D(102), high=D(104), low=D(98), close=D(100),
                   volume=1000, adj_close=D(50), source="nse_bhavcopy")  # fmt: skip
    bars, notes = to_bars([src])
    assert bars == [Bar(day(0), D(51), D(52), D(49), D(50), D(2000))] and notes == []


def test_to_bars_uses_close_when_adj_close_is_missing_and_says_so() -> None:
    src = [pbar(1, 0, "100", open="99", volume=10), pbar(1, 1, "101", adj_close="101")]
    bars, notes = to_bars(src)
    assert [b.close for b in bars] == [D(100), D(101)] and bars[0].open == D(99)
    assert notes == ["adj_close missing on 1 bar(s); close used"]
    assert bars[0].high is None and bars[0].volume == D(10)


def test_to_bars_prefers_supplied_unrounded_factors_over_the_stored_ratio() -> None:
    src = [PriceBar(security_id=1, date=day(0), close=D(3), adj_close=D("1.000000"),
                    volume=3, source="x")]  # fmt: skip
    exact = D(1) / D(3)
    bars, _ = to_bars(src, {day(0): exact})
    assert abs(bars[0].close - 1) < D("1e-20")  # exact 1/3, not the rounded 0.333333
    assert bars[0].volume is not None and abs(bars[0].volume - 9) < D("1e-20")


def test_split_adjusted_map_matches_the_old_mcp_market_helper() -> None:
    bars = [
        pbar(1, 0, "100"),
        pbar(1, 2, "50"),
        pbar(1, 3, "48", "yahoo"),
        pbar(1, 4, "49", "yahoo"),
    ]
    acts = [
        CorpAction(security_id=1, ex_date=day(1), kind="split", ratio=D(2), source="nse"),
        CorpAction(security_id=1, ex_date=day(3), kind="dividend", amount=D(1), source="nse"),
    ]

    def old(b: list[PriceBar], a: list[CorpAction]) -> dict[date, Decimal | None]:
        out: dict[date, Decimal | None] = {}
        for source in {x.source for x in b}:
            mine = [x for x in b if x.source == source]
            for x in adjust_closes(mine, a, dividends=False, splits=source != "yahoo"):
                out[x.date] = x.adj_close
        return out

    assert split_adjusted_map(bars, acts) == old(bars, acts)
    from nivesh_mcp.market import _split_adjusted

    assert _split_adjusted(bars, acts) == old(bars, acts)


# ---- security master: peers and lookups ---------------------------------------------------------
@pytest.fixture
def master(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, int]]:
    init_stores(tmp_path / "d")
    conn = open_sqlite(tmp_path / "d" / "nivesh.sqlite")
    build_master(conn, [
        mrow("AAA", industry="Widgets", sector="Industrials"),
        mrow("DDD", industry="Widgets"),
        mrow("BBB", industry="Widgets"),
        mrow("USW", "NASDAQ", market="US", currency="USD", industry="Widgets"),
        mrow("MFW", "AMFI", asset_class="mf", industry="Widgets"),
        mrow("IDXW", asset_class="index", industry="Widgets"),
        mrow("LONE", sector="Industrials"),
        mrow("NIFTY 50", asset_class="index"),
        mrow("BANKIDX", asset_class="index"),
    ], [])  # fmt: skip
    ids = {r[0]: r[1] for r in conn.execute("select symbol, id from security")}
    return conn, ids


def test_peers_same_industry_same_market_excluding_self_index_and_mf_sorted_by_symbol(
    master: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    conn, ids = master
    found = SecurityMaster(conn).peers(ids["AAA"])
    assert [r.symbol for r in found.rows] == ["BBB", "DDD"]
    assert found.basis == "industry" and found.reason is None
    assert [r.symbol for r in SecurityMaster(conn).peers(ids["AAA"], limit=1).rows] == ["BBB"]
    assert [r.id for r in load_peers(conn, ids["AAA"], AnalysisSettings()).rows] == [
        ids["BBB"], ids["DDD"],
    ]  # fmt: skip


def test_peers_override_from_config_replaces_industry_selection(
    master: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    conn, ids = master
    found = SecurityMaster(conn).peers(ids["AAA"], {"aaa": ["LONE", "ZZZ", "AAA"]})
    assert [r.symbol for r in found.rows] == ["LONE"] and found.basis == "override"
    assert found.reason is not None and "ZZZ" in found.reason
    none = SecurityMaster(conn).peers(ids["AAA"], {"AAA": ["ZZZ"]})
    assert none.rows == [] and none.reason is not None and "ZZZ" in none.reason


def test_peers_empty_with_reason_when_no_industry_and_no_override(
    master: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    conn, ids = master
    found = SecurityMaster(conn).peers(ids["LONE"])
    assert found.rows == [] and found.reason == "no industry recorded for this security"
    missing = SecurityMaster(conn).peers(99999)
    assert missing.rows == [] and missing.reason == "unknown security"


async def test_mcp_get_peers_output_unchanged_after_extraction(
    cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    monkeypatch.setenv("NIVESH_CONFIG_DIR", args[1])
    monkeypatch.setattr(common, "now", lambda: NOW)
    common._ready.clear()
    m = Mkt(data, seed_master(data))
    out = await call("fundamentals", "get_peers", security="RELIANCE")
    assert out["data"]["industry"] == "Refineries" and out["data"]["peers"] == [
        {"security_id": m.ids["ONGC"], "symbol": "ONGC", "exchange": "NSE",
         "name": "Oil and Natural Gas Corporation Limited"},
    ]  # fmt: skip
    bare = await call("fundamentals", "get_peers", security="TATAMOTORS")
    assert bare["data"]["peers"] == [] and bare["data"]["industry"] is None
    assert bare["data"]["reason"] == "no industry recorded for this security"
    capped = await call("fundamentals", "get_peers", security="RELIANCE", limit=0)
    assert len(capped["data"]["peers"]) == 1


def test_security_meta_returns_rows_by_id(
    master: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    conn, ids = master
    meta = security_meta(conn, [ids["AAA"], ids["USW"], 99999])
    assert set(meta) == {ids["AAA"], ids["USW"]}
    assert meta[ids["AAA"]].sector == "Industrials" and meta[ids["USW"]].market == "US"
    assert security_meta(conn, []) == {}


def test_sector_market_and_benchmark_lookup_from_config_or_none_with_reason(
    master: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    conn, ids = master
    sec = SecurityMaster(conn).get(ids["AAA"])
    assert sec is not None
    empty = AnalysisSettings()
    ref = benchmark_for(conn, sec, empty)
    assert ref.security_id is None and ref.symbol is None
    assert ref.reason is not None and "analysis.ta.benchmarks" in ref.reason
    cfg = AnalysisSettings.model_validate({"ta": {
        "benchmarks": {"IN": "nifty 50", "US": "NOSUCH"},
        "sector_index": {"Industrials": "BANKIDX", "Energy": "MISSING"},
    }})  # fmt: skip
    ref = benchmark_for(conn, sec, cfg)
    assert ref.security_id == ids["NIFTY 50"] and ref.symbol == "NIFTY 50" and ref.reason is None
    sector = sector_index_for(conn, sec, cfg)
    assert sector.security_id == ids["BANKIDX"] and sector.reason is None
    us = SecurityMaster(conn).get(ids["USW"])
    assert us is not None
    unknown = benchmark_for(conn, us, cfg)
    assert unknown.security_id is None and "NOSUCH" in (unknown.reason or "")
    no_sector = SecurityMaster(conn).get(ids["DDD"])
    assert no_sector is not None
    assert sector_index_for(conn, no_sector, cfg).reason == "no sector recorded for this security"
    unmapped = sector_index_for(conn, sec, AnalysisSettings())
    assert unmapped.security_id is None and "analysis.ta.sector_index" in (unmapped.reason or "")
