import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import httpx
import pytest

from nivesh_adapters.cache import cached_fetch
from nivesh_adapters.edgar import (
    Edgar,
    RateLimiter,
    extract_sections,
    parse_submissions,
    resolve_cik,
    statements,
    zero_cik,
)
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.config import Ttls
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError, RateLimited, SecretNotFound, SourceUnavailable
from nivesh_core.pii_scan import scan_paths
from nivesh_core.security_master import SecurityMaster, build_master
from tests import pii_values as pv
from tests.market_fx import accession, mrow, submissions

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
EDGAR_FX = [
    FX / n for n in ("edgar_companyfacts.json", "edgar_10k.html", "edgar_10q.html", "edgar_8k.html")
]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class Server:
    """Records requests; serves fixtures by path."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status)
        path = req.url.path
        if "companyfacts" in path:
            return httpx.Response(200, text=(FX / "edgar_companyfacts.json").read_text())
        if "submissions" in path:
            return httpx.Response(200, json=submissions(pv.cik_synthetic()))
        if path.endswith("ex8k.htm"):
            return httpx.Response(200, text=(FX / "edgar_8k.html").read_text())
        return httpx.Response(200, text=(FX / "edgar_10k.html").read_text())


@pytest.fixture
def contact(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    return pv.edgar_contact()


def edgar(server: Server, clock: FakeClock | None = None) -> Edgar:
    clk = clock or FakeClock()
    return Edgar(
        httpx.Client(transport=httpx.MockTransport(server)),
        limiter=RateLimiter(8, clk.clock, clk.sleep),
    )


def test_every_request_sends_contact_user_agent_and_accept_encoding(contact: str) -> None:
    s = Server()
    e = edgar(s)
    e.fetch(resource="companyfacts", cik="320193")
    e.fetch(resource="submissions", cik="320193")
    e.fetch(resource="document", cik="320193", accession=accession(), doc="x.htm", form="10-K")
    assert len(s.requests) == 3
    for r in s.requests:
        assert r.headers["user-agent"] == "nivesh " + contact
        assert "gzip" in r.headers["accept-encoding"]
    assert "CIK0000320193.json" in str(s.requests[0].url)


def test_missing_edgar_contact_raises_secret_not_found_with_hint(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    monkeypatch.delenv("EDGAR_CONTACT", raising=False)
    with pytest.raises(SecretNotFound, match="secrets set EDGAR_CONTACT"):
        edgar(Server()).fetch(resource="companyfacts", cik="1")


def test_contact_never_appears_in_errors_or_stored_rows(contact: str, tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailable) as err:
        edgar(Server(503)).fetch(resource="companyfacts", cik="1")
    assert contact not in str(err.value)
    conn = duckdb.connect(str(tmp_path / "c.duckdb"))
    migrate.apply(conn, MIGRATIONS / "duck")
    cached_fetch(edgar(Server()), {"resource": "submissions", "cik": "1"}, "filings",
                 conn=conn, ttls=Ttls(), redact=False)  # fmt: skip
    dump = json.dumps(conn.execute("SELECT * FROM cache_entry").fetchall(), default=str)
    assert contact not in dump


def test_rate_limiter_spaces_requests_to_max_per_sec() -> None:
    clk = FakeClock()
    lim = RateLimiter(8, clk.clock, clk.sleep)
    for _ in range(20):
        lim.wait()
    assert clk.t == pytest.approx(19 / 8)  # 20 calls take at least 19 intervals
    assert all(s == pytest.approx(1 / 8) for s in clk.slept)


def test_rate_limiter_allows_burst_below_limit() -> None:
    clk = FakeClock()
    lim = RateLimiter(10, clk.clock, clk.sleep)
    for _ in range(5):
        lim.wait()
        clk.t += 0.5  # caller already slower than the limit
    assert clk.slept == []


def test_edgar_requests_go_through_the_limiter(contact: str) -> None:
    clk = FakeClock()
    e = edgar(Server(), clk)
    for _ in range(3):
        e.fetch(resource="companyfacts", cik="1")
    assert len(clk.slept) == 2


def test_429_response_raises_rate_limited_error_not_retry_loop(contact: str) -> None:
    s = Server(429)
    with pytest.raises(RateLimited):
        edgar(s).fetch(resource="companyfacts", cik="1")
    assert len(s.requests) == 1


def test_edgar_ten_digit_cik_and_eighteen_digit_accession_url_survive_cached_fetch_twice(
    contact: str, tmp_path: Path
) -> None:
    conn = duckdb.connect(str(tmp_path / "c.duckdb"))
    migrate.apply(conn, MIGRATIONS / "duck")
    s = Server()
    params = {"resource": "submissions", "cik": pv.cik_synthetic()}
    first = cached_fetch(edgar(s), params, "filings", conn=conn, ttls=Ttls(), redact=False)
    again = cached_fetch(edgar(s), params, "filings", conn=conn, ttls=Ttls(), redact=False)
    assert len(s.requests) == 1 and first.data == again.data
    refs = parse_submissions(again.data)
    assert again.data["cik"] == pv.cik_synthetic()
    assert refs[0].accession == accession(5)
    url = refs[0].url(pv.cik_synthetic())
    assert accession(5).replace("-", "") in url and "/1234567/" in url


def test_cik_resolved_from_master_alias_and_zero_padded_in_url(tmp_path: Path) -> None:
    init_stores(tmp_path)
    sql: sqlite3.Connection = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(sql, [mrow("AAPL", "NASDAQ", market="US", currency="USD", cik="320193")], [])
    m = SecurityMaster(sql)
    sid = m.by_symbol("AAPL")[0].id
    assert resolve_cik(m, sid) == "320193" and zero_cik("320193") == "0000320193"


def test_unknown_ticker_raises_clear_error(tmp_path: Path) -> None:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(sql, [mrow("RELIANCE")], [])
    m = SecurityMaster(sql)
    with pytest.raises(NiveshError, match="no SEC CIK"):
        resolve_cik(m, m.by_symbol("RELIANCE")[0].id)


def test_companyfacts_mapped_to_standard_items_with_filed_at(contact: str) -> None:
    doc = edgar(Server()).fetch(resource="companyfacts", cik="1").data
    rows = {(r.item, r.period_type): r for r in statements(doc).rows if r.currency == "USD"}
    fy = date(2024, 9, 28)
    expect = {
        ("revenue", "A"): 391035, ("operating_income", "A"): 123216, ("net_income", "A"): 93736,
        ("cfo", "A"): 118254, ("capex", "A"): 9447, ("total_debt", "A"): 96700,
        ("total_equity", "A"): 56950, ("revenue", "Q"): 94930,
    }  # fmt: skip
    for k, v in expect.items():
        assert rows[k].value == Decimal(v) and rows[k].period_end == fy, k
        assert rows[k].filed_at == date(2024, 11, 1)
    shares = [r for r in statements(doc).rows if r.item == "shares_out"]
    assert shares and shares[0].currency == "shares"


def test_unmapped_concepts_are_ignored() -> None:
    doc = json.loads((FX / "edgar_companyfacts.json").read_text())
    assert {r.item for r in statements(doc).rows} <= {
        "revenue", "operating_income", "net_income", "cfo", "capex", "total_debt",
        "total_equity", "shares_out",
    }  # fmt: skip


def test_non_usd_unit_kept_with_currency() -> None:
    doc = json.loads((FX / "edgar_companyfacts.json").read_text())
    eur = [r for r in statements(doc).rows if r.currency == "EUR"]
    assert [(r.item, r.value) for r in eur] == [("revenue", Decimal(5000))]


def test_submissions_list_10k_10q_8k_with_filing_dates() -> None:
    refs = parse_submissions(submissions(pv.cik_synthetic()))
    assert [(r.form, r.filed_at) for r in refs] == [
        ("10-K/A", date(2024, 12, 1)), ("10-K", date(2024, 11, 1)),
        ("8-K", date(2024, 10, 31)), ("10-Q", date(2024, 8, 2)),
    ]  # fmt: skip
    assert refs[2].period_end is None and refs[1].period_end == date(2024, 9, 28)
    assert parse_submissions({}) == []


def test_10k_sections_extracted_business_risk_factors_mdna() -> None:
    s = extract_sections("10-K", (FX / "edgar_10k.html").read_text())
    assert {"business", "risk_factors", "mdna", "market_risk", "financial_statements"} <= set(s)
    assert "designs and sells widgets" in s["business"]  # body wins over the TOC entry
    assert s["mdna"].startswith("Item 7. Management's Discussion")


def test_10q_mdna_and_risk_factors() -> None:
    s = extract_sections("10-Q", (FX / "edgar_10q.html").read_text())
    assert "Quarterly revenue rose" in s["mdna"]
    assert "No material changes" in s["risk_factors"] and "None material" in s["legal_proceedings"]
    assert "Condensed" in s["financial_statements"]


def test_8k_items_by_number(contact: str) -> None:
    data = (
        edgar(Server())
        .fetch(resource="document", cik="1", accession=accession(), doc="ex8k.htm", form="8-K")
        .data
    )
    assert data["form"] == "8-K" and "announced results" in data["sections"]["item_2.02"]
    assert "item_9.01" in data["sections"]


def test_section_not_found_returns_none() -> None:
    assert extract_sections("10-K", "<p>nothing here</p>").get("mdna") is None


def test_html_script_and_style_text_removed() -> None:
    s = extract_sections("10-K", (FX / "edgar_10k.html").read_text())
    joined = " ".join(s.values())
    assert "hidden script" not in joined and "hidden style" not in joined


def test_long_section_is_truncated_and_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    import nivesh_adapters.edgar as ed

    monkeypatch.setattr(ed, "MAX_SECTION_CHARS", 30)
    s = ed.extract_sections("10-K", "<p>Item 7. MD&A</p><p>" + "word " * 40 + "</p>")
    assert s["mdna"].endswith("[section truncated]")


def test_unknown_resource_and_offline_default_client(contact: str) -> None:
    with pytest.raises(ValueError, match="unknown resource"):
        edgar(Server()).fetch(resource="nope", cik="1")
    with pytest.raises(FixtureMissing):
        Edgar().fetch(resource="companyfacts", cik="1")


def test_edgar_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths(EDGAR_FX) == []
