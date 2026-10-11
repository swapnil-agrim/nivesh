"""Brief facts from seeded stores: deterministic, read-only, gaps instead of guesses."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.brief_service import brief_input
from nivesh_core.config import Settings
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.market_store import NewsRow, write_events, write_macro, write_news
from nivesh_core.watch import track_security
from tests.analysis_fx import make_profile
from tests.ideas_fx import ASOF, BENCH, hold, seed_ideas_store

D = Decimal
RUN_AT = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
ROLES = (
    "usdinr",
    "crude",
    "y10_in",
    "y10_us",
    "policy_in",
    "policy_us",
    "vix_us",
    "fii_net",
    "dii_net",
)


def settings_of(data: Path, *, min_universe: int = 3, roles: bool = True) -> Settings:
    series = {r: {"source": "fred", "id": "X" + r} for r in ROLES} if roles else {}
    return Settings.model_validate({
        "data_dir": str(data),
        "analysis": {"ta": {"benchmarks": BENCH}, "regime": {"min_breadth_universe": min_universe}},
        "market": {"macro_series": series},
    })  # fmt: skip


@pytest.fixture
def data(tmp_path: Path) -> Path:
    seed_ideas_store(tmp_path / "d")
    return tmp_path / "d"


def facts(data: Path, market: str = "both", **kw: object):  # type: ignore[no-untyped-def]
    sql = open_sqlite(data / "nivesh.sqlite")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    try:
        return brief_input(
            duck,
            sql,
            settings_of(data, **kw),
            make_profile(),
            market,
            ASOF,
            RUN_AT,  # type: ignore[arg-type]
        )
    finally:
        sql.close()
        duck.close()


def texts(b) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(line for _, lines in b.sections for line in lines)


def macro(data: Path, role: str, value: str, day: date = ASOF) -> None:
    duck = open_duck(data / "nivesh.duckdb")
    try:
        write_macro(duck, role, [(day, D(value))], "test")
    finally:
        duck.close()


def test_brief_facts_index_levels_and_moves(data: Path) -> None:
    b = facts(data)
    assert b.table.headers[:3] == ("Market", "Index", "Level")
    assert [r[0] for r in b.table.rows] == ["India", "US"]
    assert all(r[1] in ("BENCHIN", "BENCHUS") and r[4] == ASOF.isoformat() for r in b.table.rows)
    assert all(r[3].endswith("%") for r in b.table.rows)


def test_market_argument_india_us_both(data: Path) -> None:
    assert [r[0] for r in facts(data, "india").table.rows] == ["India"]
    assert [r[0] for r in facts(data, "us").table.rows] == ["US"]
    assert "Institutional flows (India)" not in [h for h, _ in facts(data, "us").sections]
    with pytest.raises(NiveshError, match="market must be"):
        facts(data, "mars")


def test_breadth_only_when_a_universe_is_loaded_else_unavailable_with_reason(
    tmp_path: Path,
) -> None:
    seed_ideas_store(tmp_path / "n", load=False)
    gaps = dict(facts(tmp_path / "n", "india").unavailable)
    assert "no members loaded for NIFTY500" in gaps["India breadth"]


def test_regime_call_from_regime_engine(data: Path) -> None:
    b = facts(data, "india")
    text = texts(b)
    assert "India regime" in text or "India regime" in dict(b.unavailable)
    thin = dict(facts(data, "india", min_universe=50).unavailable)
    assert "breadth needs 50 securities" in thin["India regime"]


def test_rates_fx_crude_from_rates_snapshot(data: Path) -> None:
    macro(data, "usdinr", "83.25")
    macro(data, "crude", "70.5", ASOF - timedelta(days=30))
    b = facts(data)
    text = texts(b)
    assert "usdinr: 83.25 on 2026-01-02" in text
    assert "crude: 70.50" in text and "(stale)" in text
    assert "y10_in" in dict(b.unavailable)
    assert "run `nivesh market macro`" in dict(b.unavailable)["y10_in"]


def test_fii_dii_flows_from_macro_series_roles(data: Path) -> None:
    macro(data, "fii_net", "-1234.5")
    b = facts(data, "india")
    assert "FII net -1,234.50 crore on 2026-01-02" in texts(b)
    assert "DII net" in dict(b.unavailable)


def test_us_overnight_session(data: Path) -> None:
    heads = dict(facts(data, "us").sections)
    assert heads["US overnight"][0].startswith("US overnight session 2026-01-02: BENCHUS")


def test_events_and_results_for_holdings_and_watchlist(data: Path) -> None:
    hold(data, 0)
    sql = open_sqlite(data / "nivesh.sqlite")
    ids = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
    track_security(sql, ids["BBB"], None, None, ASOF)
    sql.commit()
    sql.close()
    duck = open_duck(data / "nivesh.duckdb")
    write_events(duck, [(ids["AAA"], "results", ASOF + timedelta(days=2)),
                        (ids["BBB"], "dividend", ASOF + timedelta(days=3)),
                        (ids["CCC"], "results", ASOF + timedelta(days=3)),
                        (ids["AAA"], "results", ASOF + timedelta(days=40))],
                "test", ASOF)  # fmt: skip
    duck.close()
    text = texts(facts(data))
    assert "AAA: results on 2026-01-04" in text and "BBB: dividend on 2026-01-05" in text
    assert "CCC" not in text and "2026-02" not in text
    assert "events" not in dict(facts(data).unavailable)


def test_material_news_for_holdings_only(data: Path) -> None:
    hold(data, 0)
    sql = open_sqlite(data / "nivesh.sqlite")
    ids = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
    sql.close()
    when = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)

    def item(sid: int, title: str, materiality: str) -> NewsRow:
        return NewsRow("news", when, "wire", title, None, None, "results", materiality, None,
                       "rules", sid)  # fmt: skip

    duck = open_duck(data / "nivesh.duckdb")
    write_news(duck, [item(ids["AAA"], "AAA posts record profit", "high"),
                      item(ids["AAA"], "AAA minor note", "low"),
                      item(ids["BBB"], "BBB big news", "high")])  # fmt: skip
    duck.close()
    text = texts(facts(data))
    assert "AAA: AAA posts record profit" in text
    assert "minor" not in text and "BBB" not in text


def test_no_holdings_says_so_as_a_gap(data: Path) -> None:
    gaps = dict(facts(data).unavailable)
    assert "no holdings stored" in gaps["material news"]
    assert "no holdings or watchlist entries stored" in gaps["events"]


def test_sector_leaders_laggards_unavailable_with_reason_adr_0008(data: Path) -> None:
    assert "ADR-0008" in dict(facts(data).unavailable)["sector leaders and laggards"]


def test_service_never_writes_to_stores(data: Path) -> None:
    def digest() -> list[str]:
        return [hashlib.sha256((data / n).read_bytes()).hexdigest()
                for n in ("nivesh.sqlite", "nivesh.duckdb")]  # fmt: skip

    before = digest()
    facts(data)
    assert digest() == before
