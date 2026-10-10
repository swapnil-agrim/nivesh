import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from nivesh_adapters.edgar import Edgar, RateLimiter
from nivesh_adapters.fundamentals_in import IndiaFundamentals
from nivesh_adapters.market_ingest import ingest_filings, ingest_fundamentals
from nivesh_core.config import MarketSettings, Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.market_store import (
    get_filing_sections,
    get_shareholding,
    get_statement_rows,
    list_filings,
)
from nivesh_core.security_master import SecurityMaster, SecurityRow, build_master
from nivesh_engine.statements import latest_as_of
from tests import pii_values as pv
from tests.market_fx import accession, india_six_years, mrow, submissions

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"


@dataclass
class Env:
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    settings: Settings
    master: SecurityMaster

    def sec(self, symbol: str) -> SecurityRow:
        return self.master.by_symbol(symbol)[0]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("AAPL", "NASDAQ", market="US", currency="USD", cik=pv.cik_synthetic()),
            mrow("RELIANCE", isin="INE002A01018", bse_code="500325"),
            mrow("NOBSE", isin="INE000B01011"),
            mrow("SCHEMEX", "AMFI", isin="INF000A01011", asset_class="mf"),
        ],
        [],
    )
    duck = open_duck(tmp_path / "nivesh.duckdb")
    settings = Settings(data_dir=str(tmp_path), market=MarketSettings())
    yield Env(sql, duck, settings, SecurityMaster(sql))
    duck.close()
    sql.close()


class Edge:
    """EDGAR MockTransport: company facts, submissions, documents; counts calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.facts = json.loads((FX / "edgar_companyfacts.json").read_text())
        self.fail: set[str] = set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if any(f in path for f in self.fail):
            return httpx.Response(503)
        if "companyfacts" in path:
            return httpx.Response(200, json=self.facts)
        if "submissions" in path:
            return httpx.Response(200, json=submissions(pv.cik_synthetic()))
        name = "edgar_8k.html" if path.endswith("ex8k.htm") else "edgar_10k.html"
        return httpx.Response(200, text=(FX / name).read_text())


def edgar(e: Edge) -> Any:
    c = httpx.Client(transport=httpx.MockTransport(e))
    return lambda: Edgar(c, limiter=RateLimiter(10, lambda: 0.0, lambda s: None))


class Bse:
    def __init__(self, filings: list[tuple[str, date]] | None = None) -> None:
        xml = (FX / "india_results.xbrl").read_text()
        self.filings = filings
        self.xml = xml
        self.share = json.loads((FX / "india_shareholding.json").read_text())
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if any(f in path for f in self.fail):
            return httpx.Response(503)
        if "ShareHolding" in path:
            return httpx.Response(200, json=self.share)
        if "ResultsXbrl" in path:
            if self.filings is None:
                one = {"period_end": "2025-12-31", "filed_at": "2026-01-20",
                       "xbrl_url": "https://example.invalid/r0.xml"}  # fmt: skip
                return httpx.Response(200, json=[one])
            rows = [
                {
                    "period_end": d.isoformat(),
                    "filed_at": d.isoformat(),
                    "xbrl_url": f"https://example.invalid/r{i}.xml",
                }  # fmt: skip
                for i, (_x, d) in enumerate(self.filings)
            ]
            return httpx.Response(200, json=rows)
        if self.filings is not None:
            i = int(path.rsplit("/r", 1)[1].removesuffix(".xml"))
            return httpx.Response(200, text=self.filings[i][0])
        return httpx.Response(200, text=self.xml)


def india(b: Bse) -> Any:
    c = httpx.Client(transport=httpx.MockTransport(b))
    return lambda: IndiaFundamentals(c)


def test_ingest_fundamentals_us_stores_rows_with_filed_at_and_depth(env: Env) -> None:
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"),
                              edgar=edgar(Edge()))  # fmt: skip
    rows = get_statement_rows(env.duck, rep.security_id)
    assert rep.rows == len(rows) > 0 and rep.failed == [] and not rep.stale
    assert {r.item for r in rows} >= {"revenue", "net_income", "total_equity"}
    assert all(r.filed_at is not None for r in rows)
    assert rep.annual_periods >= 1 and rep.quarterly_periods >= 1
    src = env.duck.execute("select distinct source from fundamental").fetchall()
    assert src == [("sec_edgar",)]


def test_ingest_fundamentals_in_stores_statements_and_shareholding(env: Env) -> None:
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("RELIANCE"),
                              india=india(Bse()))  # fmt: skip
    rows = get_statement_rows(env.duck, rep.security_id)
    assert {r.item for r in rows} >= {"revenue", "net_income"}
    assert all(r.filed_at == date(2026, 1, 20) for r in rows)
    sh = get_shareholding(env.duck, rep.security_id)
    assert [s.period_end for s in sh] == [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
    assert rep.shareholding == 3
    assert env.duck.execute("select distinct source from fundamental").fetchall() == [("bse_xbrl",)]


def test_india_depth_targets_met_over_six_years(env: Env) -> None:
    b = Bse(india_six_years())
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("RELIANCE"),
                              india=india(b))  # fmt: skip
    assert rep.annual_periods >= 5 and rep.quarterly_periods >= 12


def test_reingest_same_filing_is_idempotent_but_restatement_adds_rows(env: Env) -> None:
    e = Edge()
    first = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    n = len(get_statement_rows(env.duck, first.security_id))
    again = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"),
                                edgar=edgar(e), refresh=True)  # fmt: skip
    assert len(get_statement_rows(env.duck, first.security_id)) == n and again.rows == 0
    # a later filing restates FY revenue: a new row with a later filed_at, history kept
    rev = e.facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    rev["units"]["USD"].append(
        {
            "start": "2023-10-01",
            "end": "2024-09-28",
            "val": 391036,
            "filed": "2025-02-01",
            "form": "10-K/A",
        }  # fmt: skip
    )
    again = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"),
                                edgar=edgar(e), refresh=True)  # fmt: skip
    assert again.rows == 1
    rows = get_statement_rows(env.duck, first.security_id)
    assert len(rows) == n + 1

    def revenue(as_of: date) -> int:
        got = [r for r in latest_as_of(rows, as_of) if r.item == "revenue" and r.period_type == "A"]
        return int(got[0].value)

    assert revenue(date(2025, 1, 1)) == 391035 and revenue(date(2025, 3, 1)) == 391036


def test_fundamentals_ttl_used_and_stale_flag_on_source_failure(env: Env) -> None:
    e = Edge()
    ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    n = len(e.calls)
    ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    assert len(e.calls) == n  # inside the TTL: served from cache
    e.fail = {"companyfacts"}
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"),
                              edgar=edgar(e), refresh=True)  # fmt: skip
    assert rep.stale is True


def test_failed_source_without_cache_is_reported_not_fatal(env: Env) -> None:
    e = Edge()
    e.fail = {"companyfacts"}
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    assert rep.rows == 0 and any("companyfacts" in f for f in rep.failed)
    b = Bse()
    b.fail = {"ResultsXbrl", "ShareHolding"}
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("RELIANCE"),
                              india=india(b))  # fmt: skip
    assert rep.rows == 0 and len(rep.failed) == 2


def test_ingest_fundamentals_rejects_unsupported_securities(env: Env) -> None:
    with pytest.raises(NiveshError, match="mutual fund"):
        ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("SCHEMEX"))
    with pytest.raises(NiveshError, match="BSE scrip code"):
        ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("NOBSE"), india=india(Bse()))


def test_filing_text_stored_by_section_and_retrievable(env: Env) -> None:
    e = Edge()
    rep = ingest_filings(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    assert rep.listed == 4 and rep.with_sections == 4 and rep.failed == []
    filings = list_filings(env.duck, rep.security_id)
    assert [f.form for f in filings] == ["10-K/A", "10-K", "8-K", "10-Q"]  # newest first
    ten_k = next(f for f in filings if f.form == "10-K")
    sections = get_filing_sections(env.duck, ten_k.id)
    assert {"business", "risk_factors", "mdna"} <= set(sections)
    assert ten_k.url is not None and accession(1).replace("-", "") in ten_k.url
    again = ingest_filings(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    assert again.listed == 4
    assert env.duck.execute("select count(*) from filing").fetchone() == (4,)


def test_ingest_filings_fetches_documents_only_for_requested_forms(env: Env) -> None:
    e = Edge()
    rep = ingest_filings(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e),
                         forms=("8-K",))  # fmt: skip
    assert [c for c in e.calls if c.endswith(".htm")] == [c for c in e.calls if "ex8k" in c]
    assert rep.listed == rep.with_sections == 1


def test_ingest_filings_per_form_cap_limits_document_fetches(env: Env) -> None:
    e = Edge()
    rep = ingest_filings(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e),
                         per_form=0)  # fmt: skip
    assert rep.listed == 4 and rep.with_sections == 0
    assert not [c for c in e.calls if c.endswith(".htm")]


def test_ingest_filings_requires_a_us_listing(env: Env) -> None:
    with pytest.raises(NiveshError, match="SEC CIK"):
        ingest_filings(env.duck, env.sql, env.settings, env.sec("RELIANCE"))


def test_edgar_contact_never_stored(env: Env) -> None:
    e = Edge()
    ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    ingest_filings(env.duck, env.sql, env.settings, env.sec("AAPL"), edgar=edgar(e))
    dump = json.dumps(env.duck.execute("select * from cache_entry").fetchall(), default=str)
    assert pv.edgar_contact() not in dump
