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
from nivesh_adapters.fundamentals_tapetide import TapetideFundamentals
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
from tests import tapetide_fx as tx
from tests.conftest import MemoryKeyring
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
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> Iterator[Env]:
    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("AAPL", "NASDAQ", market="US", currency="USD", cik=pv.cik_synthetic()),
            mrow("RELIANCE", isin="INE002A01018", bse_code="500325"),
            mrow("NOBSE", isin="INE000B01011"),
            mrow("HDFCBANK", isin="INE040A01034"),
            mrow("TCS", isin="INE467B01029"),
            mrow("INFY", isin="INE009A01021"),
            mrow("ITC", isin="INE154A01025"),
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


TODAY = date(2026, 10, 13)


def no_token() -> Any:
    """Tapetide factory with no token set: unavailable, and it must not touch the network."""
    srv = tx.Tapetide()
    return lambda: tx.adapter(srv)


def with_token(monkeypatch: pytest.MonkeyPatch, srv: tx.Tapetide) -> Any:
    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token())
    return lambda: tx.adapter(srv)


def run_in(env: Env, symbol: str, tapetide: Any, **kw: Any) -> Any:
    return ingest_fundamentals(env.duck, env.sql, env.settings, env.sec(symbol),
                               tapetide=tapetide, today=TODAY, **kw)  # fmt: skip


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
                              india=india(Bse()), tapetide=no_token())  # fmt: skip
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
                              india=india(b), tapetide=no_token())  # fmt: skip
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
                              india=india(b), tapetide=no_token())  # fmt: skip
    assert rep.rows == 0 and len(rep.failed) == 3  # Tapetide token, BSE results, BSE shareholding


def test_ingest_fundamentals_rejects_unsupported_securities(env: Env) -> None:
    with pytest.raises(NiveshError, match="mutual fund"):
        ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("SCHEMEX"))


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


# India via Tapetide (ADR-0015) -----------------------------------------------------------------


def test_india_ingest_uses_tapetide_without_bse_code(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    srv.aliases["NOBSE"] = "RELIANCE"  # a listing with only an NSE symbol and no BSE scrip code
    rep = run_in(env, "NOBSE", with_token(monkeypatch, srv))
    assert rep.annual_periods >= 5 and rep.quarterly_periods == 13 and rep.failed == []
    assert env.duck.execute("select distinct source from fundamental order by 1").fetchall() == [
        ("tapetide",), ("tapetide_est",),
    ]  # fmt: skip
    assert rep.tapetide_calls == 6 == srv.calls


def test_india_ingest_prefers_tapetide_and_skips_bse_when_it_succeeds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = Bse()
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, tx.Tapetide()), india=india(b))
    assert b.calls == [] and rep.rows > 0
    assert env.duck.execute("select count(*) from fundamental where source = 'bse_xbrl'"
                            ).fetchone() == (0,)  # fmt: skip


def test_falls_back_to_bse_when_tapetide_unavailable(env: Env) -> None:
    srv = tx.Tapetide()
    rep = run_in(env, "RELIANCE", lambda: tx.adapter(srv), india=india(Bse()))
    assert srv.calls == 0  # no token: not one request
    assert any("TAPETIDE_TOKEN" in f for f in rep.failed)
    assert env.duck.execute("select distinct source from fundamental").fetchall() == [("bse_xbrl",)]
    assert rep.rows > 0 and rep.shareholding == 3


def test_no_crash_when_both_sources_unavailable(env: Env) -> None:
    rep = run_in(env, "NOBSE", no_token(), india=india(Bse()))
    assert rep.rows == 0 and rep.annual_periods == 0
    assert any("TAPETIDE_TOKEN" in f for f in rep.failed)
    assert any("BSE scrip code" in f for f in rep.failed)


def test_rejected_token_halts_the_whole_ingest_after_one_request(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    srv.fail_all = httpx.Response(401, json=srv.misc["err_bad_token_401"])
    rep = run_in(env, "NOBSE", with_token(monkeypatch, srv))
    assert srv.calls == 1 and rep.tapetide_calls == 0
    assert len([f for f in rep.failed if "rejected the token" in f]) == 1  # one line, not six
    assert tx.token() not in json.dumps(rep.failed)


def test_one_failed_section_does_not_block_others(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    srv.fail_sections["cash_flow"] = httpx.Response(200, text="<html>bad gateway</html>")
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert {"revenue", "total_equity", "shares_out"} <= items and not items & {"cfo", "capex"}
    assert len(rep.failed) == 1 and "cash_flow" in rep.failed[0]
    assert rep.shareholding > 0 and any("cash_flow" in n for n in rep.notes)


def test_quota_exhaustion_reports_reason_and_keeps_cached_rows(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    first = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    n = len(get_statement_rows(env.duck, first.security_id))
    denied = tx.envelope("Error: Rate limit exceeded", is_error=True)
    srv.fail_all = httpx.Response(200, json=denied, headers={"Retry-After": "60"})
    again = run_in(env, "RELIANCE", with_token(monkeypatch, srv), refresh=True)
    assert again.stale and again.rows == 0
    assert len(get_statement_rows(env.duck, first.security_id)) == n
    fresh = run_in(env, "HDFCBANK", with_token(monkeypatch, srv))  # nothing cached for this one
    quota = [f for f in fresh.failed if "50 calls/day" in f]
    assert fresh.rows == 0 and len(quota) == 1  # one reason line, not one per section


def test_quota_denial_mid_run_stops_further_calls_and_keeps_what_was_fetched(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    srv.quota_after = 2  # quarterly and profit_loss land, the third call is denied
    srv.aliases["NOBSE"] = "RELIANCE"
    rep = run_in(env, "NOBSE", with_token(monkeypatch, srv))
    assert srv.calls == 3 and rep.tapetide_calls == 2  # no snapshot, no shareholding requested
    assert rep.rows > 0 and rep.annual_periods > 0 and rep.quarterly_periods == 13
    assert len([f for f in rep.failed if "50 calls/day" in f]) == 1
    assert any("missing Tapetide sections" in n for n in rep.notes)
    assert not [r for r in get_statement_rows(env.duck, rep.security_id) if r.item == "shares_out"]


def test_second_ingest_makes_zero_network_calls(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = tx.Tapetide()
    factory = with_token(monkeypatch, srv)
    run_in(env, "RELIANCE", factory)
    again = run_in(env, "RELIANCE", factory)
    assert srv.calls == 6 and again.tapetide_calls == 0 and again.rows == 0


def test_report_notes_minority_interest_when_eps_times_shares_differs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    factory = with_token(monkeypatch, srv)
    has_note = {
        c: any("minority interest" in n for n in run_in(env, c, factory).notes)
        for c in ("RELIANCE", "HDFCBANK", "TCS", "INFY", "ITC")
    }
    # RELIANCE 18.5% and HDFCBANK 4.2% above the attributable figure; the others within 3%
    assert has_note == {"RELIANCE": True, "HDFCBANK": True, "TCS": False, "INFY": False,
                        "ITC": False}  # fmt: skip


def test_report_counts_estimated_rows_and_clamped_dates(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = with_token(monkeypatch, tx.Tapetide())
    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("RELIANCE"),
                              tapetide=factory, today=date(2024, 5, 1))  # fmt: skip
    est = env.duck.execute(
        "select count(*) from fundamental where source = 'tapetide_est'"
    ).fetchone()
    assert est is not None and rep.estimated_rows == est[0] > 0
    assert rep.clamped_rows > 0 and any("estimated filing date" in n for n in rep.notes)
    assert env.duck.execute("select max(filed_at) from fundamental").fetchone() == (
        date(2024, 5, 1),
    )


def test_shareholding_is_stored_as_estimated_with_latest_pledge(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, tx.Tapetide()))
    sh = get_shareholding(env.duck, rep.security_id)
    assert len(sh) == rep.shareholding == 8 and sh[-1].promoter_pledged_pct == 0
    assert env.duck.execute("select distinct source from shareholding").fetchall() == [
        ("tapetide_est",)
    ]


def test_a_declared_filing_replaces_the_earlier_estimate_of_the_same_period(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()

    def estimated(sec: dict[str, Any]) -> None:
        for a in sec["availability"]:
            if a["period"] == "Mar 2026":
                a.update(basis="estimated", available_from="2026-05-31")

    srv.edit("RELIANCE", "profit_loss", estimated)
    factory = with_token(monkeypatch, srv)
    run_in(env, "RELIANCE", factory)
    q = "select source, filed_at from fundamental where item = 'revenue' and period_end = ? "
    q += "and period_type = 'A'"
    assert env.duck.execute(q, (date(2026, 3, 31),)).fetchall() == [
        ("tapetide_est", date(2026, 5, 31))
    ]
    srv.files["RELIANCE"] = tx.Tapetide().files["RELIANCE"]  # the vendor now has the real date
    run_in(env, "RELIANCE", factory, refresh=True)
    assert env.duck.execute(q, (date(2026, 3, 31),)).fetchall() == [("tapetide", date(2026, 4, 27))]


def test_us_path_does_not_touch_tapetide(env: Env) -> None:
    def boom() -> Any:
        raise AssertionError("Tapetide is for India only")

    rep = ingest_fundamentals(env.duck, env.sql, env.settings, env.sec("AAPL"),
                              edgar=edgar(Edge()), tapetide=boom)  # fmt: skip
    assert rep.rows > 0 and rep.tapetide_calls is None and rep.notes == []


def test_token_never_reaches_the_store_or_the_report(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, tx.Tapetide()))
    dump = json.dumps(env.duck.execute("select * from cache_entry").fetchall(), default=str)
    assert tx.token() not in dump + repr(rep)
    assert TapetideFundamentals.source == "tapetide"


# Malformed bodies that pass validate() must end as a failed-source line, never a traceback ------


def _cash_flow_scalar_cell(sec: dict[str, Any]) -> None:
    sec["data"]["Cash from Operating Activity"] = 5  # a row value that is no period map


def _cash_flow_numeric_period(sec: dict[str, Any]) -> None:
    sec["periods"] = [5]


@pytest.mark.parametrize("change", [_cash_flow_scalar_cell, _cash_flow_numeric_period])
def test_malformed_section_body_is_a_failed_source_line_and_others_still_land(
    env: Env, monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    srv = tx.Tapetide()
    srv.edit("RELIANCE", "cash_flow", change)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert {"revenue", "total_equity"} <= items and not items & {"cfo", "capex"}
    assert len(rep.failed) == 1 and "cash_flow" in rep.failed[0]
    assert "unexpected response shape" in rep.failed[0] and rep.shareholding > 0


def test_malformed_shareholding_rows_are_a_failed_source_line_and_statements_still_land(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    payload = tx.inner(srv.misc["shareholding"])
    payload["data"][0]["data"] = {"Promoters": 50.5, "Public": 49.5}
    srv.misc["shareholding"] = tx.envelope(payload)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert rep.rows > 0 and rep.shareholding == 0
    assert len(rep.failed) == 1 and "shareholding" in rep.failed[0]
    assert "unexpected response shape" in rep.failed[0]


def test_iserror_text_echoing_the_token_never_reaches_the_failed_lines(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    echo = f"Error: bad credential {tx.token()} supplied"
    srv.fail_all = httpx.Response(200, json=tx.envelope(echo, is_error=True))
    rep = run_in(env, "NOBSE", with_token(monkeypatch, srv))
    assert rep.failed and "Tapetide error" in json.dumps(rep.failed)
    assert tx.token() not in json.dumps(rep.failed)


# Hostile payloads: every one ends as failed-source lines with reasons, never an exception ------


def _edit_misc(srv: tx.Tapetide, key: str, change: Any) -> None:
    payload = tx.inner(srv.misc[key])
    change(payload)
    srv.misc[key] = tx.envelope(payload)


def _holding(change: Any) -> Any:
    return lambda srv: _edit_misc(srv, "shareholding", change)


def _set_promoter(v: Any) -> Any:
    return _holding(lambda p: p["data"][0]["data"]["Promoters"].update({"Mar 2026": v}))


def _set_pledge(v: Any) -> Any:
    return _holding(lambda p: p.update(promoter_pledge=v))


def _pledge_pct(v: Any) -> Any:
    return _holding(lambda p: p["promoter_pledge"].update(pct_of_promoter_holding=v))


def _snapshot(v: Any) -> Any:
    return lambda srv: _edit_misc(
        srv, "snapshot_reliance", lambda p: p["data"][0]["data"].update({"Face Value": v})
    )


def _sales(v: Any) -> Any:
    def edit(srv: tx.Tapetide) -> None:
        srv.edit("RELIANCE", "profit_loss", lambda s: s["data"]["Sales"].update({"Mar 2026": v}))

    return edit


def _avail(v: Any) -> Any:
    return lambda srv: srv.edit("RELIANCE", "cash_flow", lambda s: s.update(availability=v))


def _year_zero(srv: tx.Tapetide) -> None:
    def edit(s: dict[str, Any]) -> None:
        s["periods"].append("Mar 0000")
        s["availability"].append({"period": "Mar 0000", "available_from": "2026-04-27",
                                  "basis": "reported"})  # fmt: skip

    srv.edit("RELIANCE", "cash_flow", edit)


# (name, edit, failed-line part, what must still land: statements / shareholding)
HOSTILE_HOLDING = [
    ("list_cell", _set_promoter([1, 2]), "Promoters", True),
    ("string_cell", _set_promoter("50.1%"), "Promoters", True),
    ("huge_pct", _set_promoter(1e9), "Promoters", True),
    ("pledge_scalar", _set_pledge(7), "promoter_pledge", True),
    ("pledge_string_pct", _pledge_pct("abc"), "pct_of_promoter_holding", True),
    ("pledge_huge_pct", _pledge_pct(1e9), "pct_of_promoter_holding", True),
    ("pledge_over_100", _pledge_pct(150), "pct_of_promoter_holding", True),
    ("pledge_negative", _pledge_pct(-5), "pct_of_promoter_holding", True),
]
HOSTILE_STATEMENT = [
    ("string_sales", _sales("1,000"), "profit_loss", True),
    ("huge_sales", _sales(10**30), "profit_loss", True),
    ("list_sales", _sales([1]), "profit_loss", True),
    ("scalar_availability", _avail(5), "cash_flow", True),
    ("availability_not_dicts", _avail([1, "x"]), "cash_flow", True),
]


@pytest.mark.parametrize(("name", "edit", "part", "_"), HOSTILE_HOLDING + HOSTILE_STATEMENT)
def test_hostile_payload_is_a_failed_line_and_other_calls_still_land(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str, edit: Any, part: str, _: bool
) -> None:
    srv = tx.Tapetide()
    edit(srv)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert len(rep.failed) == 1 and part in rep.failed[0] and ": " in rep.failed[0], name
    rows = get_statement_rows(env.duck, rep.security_id)
    items = {r.item for r in rows}
    assert "total_equity" in items and "shares_out" in items
    if name.endswith("sales"):
        assert not any(r.item == "revenue" and r.period_type == "A" for r in rows)
        assert "revenue" in items  # the quarterly section still landed
    else:
        assert "revenue" in items
    if (name, edit, part, _) in HOSTILE_HOLDING:
        assert rep.shareholding == 0
    else:
        assert rep.shareholding > 0
    cached = json.dumps(env.duck.execute("select * from cache_entry").fetchall(), default=str)
    assert "1,000" not in cached and "50.1%" not in cached  # bad data is never cached


def test_bad_snapshot_loses_only_the_share_count(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    srv = tx.Tapetide()
    _snapshot("Rs 10")(srv)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert {"revenue", "total_equity"} <= items and "shares_out" not in items
    assert len(rep.failed) == 1 and "snapshot" in rep.failed[0]
    assert rep.shareholding > 0


@pytest.mark.parametrize("fv", [1e-30, 1e30, 0])
def test_absurd_face_value_gives_no_share_count_not_an_error(
    env: Env, monkeypatch: pytest.MonkeyPatch, fv: float
) -> None:
    srv = tx.Tapetide()
    _snapshot(fv)(srv)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert "revenue" in items and "shares_out" not in items and rep.rows > 0
    assert any("shares_out" in s for s in rep.skipped) or any("snapshot" in f for f in rep.failed)


def test_period_label_with_year_zero_is_ignored_not_fatal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    _year_zero(srv)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert rep.failed == [] and rep.rows > 0
    assert all(r.period_end.year > 1 for r in get_statement_rows(env.duck, rep.security_id))


def test_a_filing_date_before_the_period_end_is_clamped_up_and_reported(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()

    def early(s: dict[str, Any]) -> None:
        for a in s["availability"]:
            if a["period"] == "Mar 2026":
                a["available_from"] = "1999-01-01"

    srv.edit("RELIANCE", "profit_loss", early)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    q = "select min(filed_at - period_end) from fundamental"
    row = env.duck.execute(q).fetchone()
    assert row is not None and row[0] >= 0
    assert any("before the period end" in n for n in rep.notes)


def test_a_failed_write_is_a_failed_line_and_leaves_nothing_behind(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nivesh_adapters.market_ingest as mi

    def boom(*a: Any, **k: Any) -> None:
        raise ArithmeticError("overflow")

    monkeypatch.setattr(mi, "write_fundamentals", boom)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, tx.Tapetide()))
    assert rep.rows == 0 and any("overflow" in f for f in rep.failed)


def test_rows_counts_inserted_rows_when_a_report_supersedes_an_estimate(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()

    def estimated(sec: dict[str, Any]) -> None:
        for a in sec["availability"]:
            a.update(basis="estimated")

    srv.edit("RELIANCE", "profit_loss", estimated)
    factory = with_token(monkeypatch, srv)
    q = "select count(*) from fundamental where source = 'tapetide'"
    run_in(env, "RELIANCE", factory)
    before = env.duck.execute(q).fetchone()
    srv.files["RELIANCE"] = tx.Tapetide().files["RELIANCE"]
    second = run_in(env, "RELIANCE", factory, refresh=True)
    after = env.duck.execute(q).fetchone()
    assert before is not None and after is not None and after[0] > before[0]
    assert second.rows == after[0] - before[0]  # the estimates deleted do not offset the inserts


def test_write_is_all_or_none_when_one_row_overflows(env: Env) -> None:
    from decimal import Decimal

    from nivesh_core.market_store import write_fundamentals
    from nivesh_engine.statements import StatementRow

    def row(item: str, v: Decimal) -> StatementRow:
        return StatementRow(period_end=date(2026, 3, 31), period_type="A", item=item, value=v,
                            currency="INR", filed_at=date(2026, 4, 27))  # fmt: skip

    with pytest.raises(duckdb.Error):
        write_fundamentals(
            env.duck,
            1,
            [row("revenue", Decimal(5)), row("net_income", Decimal(10) ** 30)],
            "tapetide",
        )
    assert env.duck.execute("select count(*) from fundamental").fetchone() == (0,)


# review round 2 -------------------------------------------------------------------------------


def _bad_chars(lines: list[str]) -> list[str]:
    import unicodedata

    return [c for c in "".join(lines) if unicodedata.category(c).startswith("C")]


def test_real_shareholding_with_the_count_row_lands_in_the_store(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, tx.Tapetide()))
    assert rep.failed == [] and rep.shareholding == 8


@pytest.mark.parametrize("where", ["quarterly", "snapshot", "shareholding"])
def test_deeply_nested_bodies_are_failed_lines_not_a_crash(
    env: Env, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    srv = tx.Tapetide()
    deep = "[" * 100_000
    if where == "quarterly":
        srv.files["RELIANCE"]["quarterly"] = tx.envelope(deep)
    else:
        key = "snapshot_reliance" if where == "snapshot" else "shareholding"
        srv.misc[key] = tx.envelope('{"data":' + deep)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert len(rep.failed) == 1 and "unexpected response shape" in rep.failed[0]
    assert rep.rows > 0


@pytest.mark.parametrize("fv", [{"x": 1}, [10], {"a": {"b": 1}}])
def test_dict_or_list_face_value_loses_only_the_share_count(
    env: Env, monkeypatch: pytest.MonkeyPatch, fv: Any
) -> None:
    srv = tx.Tapetide()
    _snapshot(fv)(srv)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert {"revenue", "total_equity"} <= items and "shares_out" not in items
    assert not any("statements" in f for f in rep.failed) and rep.shareholding > 0


def test_a_text_row_in_the_snapshot_does_not_cost_the_share_count(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    _edit_misc(srv, "snapshot_reliance", lambda p: p["data"][0]["data"].update({"Sector": "Oil"}))
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert "shares_out" in items and rep.failed == []


def test_face_value_inside_bounds_but_absurd_share_count_gives_none(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    _snapshot(0.0001)(srv)  # the lowest accepted face value: capital / 0.0001 is above the cap
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    items = {r.item for r in get_statement_rows(env.duck, rep.security_id)}
    assert "revenue" in items and "shares_out" not in items and rep.failed == []
    assert any("implausible share count" in s for s in rep.skipped)


def test_future_period_is_skipped_and_year_9999_shareholding_loses_only_itself(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()

    def add(p: dict[str, Any]) -> None:
        for label in ("Dec 9999", "Mar 2030"):
            p["data"][0]["data"]["Promoters"][label] = 50.0
            p["data"][0]["data"]["Public"][label] = 9.0

    _edit_misc(srv, "shareholding", add)

    def future(s: dict[str, Any]) -> None:
        s["periods"].append("Mar 2030")
        s["data"]["Sales"]["Mar 2030"] = 5
        s["availability"].append({"period": "Mar 2030", "available_from": "2030-05-01",
                                  "basis": "reported"})  # fmt: skip

    srv.edit("RELIANCE", "profit_loss", future)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert rep.failed == [] and rep.shareholding == 8
    assert any("Dec 9999" in s for s in rep.skipped) and any("Mar 2030" in s for s in rep.skipped)
    assert all(r.period_end.year < 2030 for r in get_statement_rows(env.duck, rep.security_id))


def test_shareholding_without_promoter_public_rows_is_a_skipped_reason(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()
    _holding(lambda p: p["data"][0].update(data={"FIIs": {"Jun 2026": 17.2}}))(srv)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert rep.shareholding == 0 and any("Promoters" in s for s in rep.skipped)


@pytest.mark.parametrize(
    "exc", [ArithmeticError("x"), ValueError("x"), TypeError("x"), duckdb.ConversionException("x")]
)
def test_every_store_failure_type_is_a_failed_line(
    env: Env, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    import nivesh_adapters.market_ingest as mi

    def boom(*a: Any, **k: Any) -> None:
        raise exc

    monkeypatch.setattr(mi, "write_fundamentals", boom)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, tx.Tapetide()))
    assert rep.rows == 0 and any("could not store data" in f for f in rep.failed)


def test_token_with_a_trailing_newline_works_and_never_reaches_the_report(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = with_token(monkeypatch, tx.Tapetide())
    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token() + "\n")
    rep = run_in(env, "RELIANCE", factory)
    assert rep.rows > 0 and rep.failed == []
    assert tx.token() not in json.dumps([rep.failed, rep.notes, rep.skipped])


def test_a_transport_error_naming_the_token_is_scrubbed_from_the_report(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {tx.token()}\\x00'")

    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token() + "\x07")
    rep = run_in(env, "NOBSE", lambda: tx.adapter(boom))
    assert rep.failed and tx.token() not in json.dumps(rep.failed)


def test_vendor_text_is_stripped_of_control_characters_in_failed_lines(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    nasty = "Error: \x1b[31mred\x1b[0m ‮txet‬ \x85 \x07bell end"
    srv = tx.Tapetide()
    srv.fail_all = httpx.Response(200, json=tx.envelope(nasty, is_error=True))
    rep = run_in(env, "NOBSE", with_token(monkeypatch, srv))
    assert rep.failed and "Tapetide error" in rep.failed[0] and _bad_chars(rep.failed) == []


def test_row_and_period_names_from_the_vendor_are_stripped_in_failed_lines(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = tx.Tapetide()

    def rename(s: dict[str, Any]) -> None:
        s["data"]["Sa\x1b[2Jles‮"] = {"Mar 2026": "1,000"}

    srv.edit("RELIANCE", "profit_loss", rename)
    rep = run_in(env, "RELIANCE", with_token(monkeypatch, srv))
    assert any("profit_loss" in f and "bad" in f for f in rep.failed)
    assert _bad_chars(rep.failed) == []
