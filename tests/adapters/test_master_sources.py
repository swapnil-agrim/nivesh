from datetime import date
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.master_sources import (
    URLS,
    MasterSources,
    default_indices,
    parse_amfi,
    parse_bse,
    parse_nse,
    parse_sec,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import SecretNotFound, SourceUnavailable
from nivesh_core.pii_scan import scan_paths
from tests import pii_values as pv

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"


def text(name: str) -> str:
    return (FX / name).read_text()


def test_parse_nse_equity_list_maps_symbol_name_isin_exchange_nse() -> None:
    rows = {r.symbol: r for r in parse_nse(text("nse_equity_l.csv"))}
    r = rows["RELIANCE"]
    assert (r.exchange, r.isin, r.name, r.currency, r.market) == (
        "NSE", "INE002A01018", "Reliance Industries Limited", "INR", "IN",
    )  # fmt: skip


def test_parse_nse_skips_non_equity_series_and_blank_isin() -> None:
    syms = {r.symbol for r in parse_nse(text("nse_equity_l.csv"))}
    assert "DEBTCO" not in syms and "NOISIN" not in syms and "TATASTEEL" in syms


def test_parse_bse_udiff_gives_symbol_name_isin_bse_code() -> None:
    rows = {r.symbol: r for r in parse_bse(text("bse_scrips.csv"))}
    r = rows["RELIANCE"]
    assert (r.bse_code, r.isin, r.exchange, r.name) == (
        "500325", "INE002A01018", "BSE", "RELIANCE INDUSTRIES LTD.",
    )  # fmt: skip
    assert r.industry is None  # the bhavcopy carries no industry
    assert rows["TCS"].bse_code == "532540" and rows["SBIN"].bse_code == "500112"


def test_parse_bse_skips_blank_isin_and_non_equity_groups() -> None:
    # live 2026-10-13 file: groups A/B/T/X/XT/M/MT/Z/ZP/E/IF/P/MS/TS carry equity-like scrips;
    # F (debt, bonds, ETFs of AMC debt funds), G (gilts), W (warrants), R (rights) are dropped
    syms = {r.symbol for r in parse_bse(text("bse_scrips.csv"))}
    assert syms == {"RELIANCE", "TCS", "SBIN"}  # NOISIN has no ISIN, 7NCD30 is group F


def test_parse_bse_missing_udiff_column_raises() -> None:
    with pytest.raises(DataQualityError, match="missing column"):
        parse_bse("TckrSymb,ISIN\nAAA,INE000A01010\n")


def test_amfi_url_is_portal_host() -> None:
    assert URLS["amfi"] == "https://portal.amfiindia.com/spages/NAVAll.txt"


def test_parse_amfi_navall_yields_one_row_per_scheme_isin_with_amfi_code_and_symbol_isin() -> None:
    rows = parse_amfi(text("amfi_navall.txt"))
    assert [(r.symbol, r.amfi_code) for r in rows] == [
        ("INF000A01011", "100001"), ("INF000A01029", "100001"), ("INF111B01012", "100002"),
    ]  # fmt: skip
    assert all(r.exchange == "AMFI" and r.asset_class == "mf" and r.isin == r.symbol for r in rows)


def test_parse_sec_tickers_maps_ticker_cik_name_exchange_and_market_us() -> None:
    rows = {r.symbol: r for r in parse_sec(text("sec_tickers_exchange.json"))}
    assert rows["AAPL"].cik == "320193" and rows["AAPL"].exchange == "NASDAQ"
    assert rows["XOM"].exchange == "NYSE" and rows["AAPL"].market == "US"
    assert rows["AAPL"].currency == "USD" and rows["AAPL"].name == "Apple Inc."


def test_default_indices_listed_with_asset_class_index() -> None:
    idx = default_indices()
    assert {r.symbol for r in idx} >= {"NIFTY 50", "SENSEX", "INDIA VIX", "^GSPC"}
    assert all(r.asset_class == "index" and r.isin is None for r in idx)


@pytest.mark.parametrize(
    "fn, bad, where",
    [
        (parse_amfi, "abc;INF000A01011;-;Fund;1;d", "line 1"),
        (parse_sec, '{"fields": ["cik"], "data": []}', "document"),
        (parse_sec, '{"fields": ["cik","name","ticker","exchange"], "data": [[1]]}', "record 0"),
        (parse_nse, "SYMBOL,SERIES\nAAA,EQ\n", "line 2"),
        (parse_nse, "", "header"),
    ],
)
def test_malformed_row_raises_data_quality_error_naming_source_and_line(
    fn: object, bad: str, where: str
) -> None:
    with pytest.raises(DataQualityError, match=where):
        fn(bad)  # type: ignore[operator]


def test_master_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths([FX]) == []


def client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_fetch_returns_text_and_sec_sends_contact_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text="body")

    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    ms = MasterSources(client(handler))
    assert ms.fetch(resource="sec").data == "body"
    assert pv.edgar_contact() in seen[0].headers["user-agent"]
    ms.fetch(resource="nse")
    assert pv.edgar_contact() not in seen[1].headers["user-agent"]


def test_sec_without_contact_secret_fails_with_hint(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    monkeypatch.delenv("EDGAR_CONTACT", raising=False)
    with pytest.raises(SecretNotFound, match="secrets set EDGAR_CONTACT"):
        MasterSources(client(lambda r: httpx.Response(200))).fetch(resource="sec")


def test_http_error_status_is_source_unavailable() -> None:
    with pytest.raises(SourceUnavailable, match="503"):
        MasterSources(client(lambda r: httpx.Response(503))).fetch(resource="nse")


def test_default_client_replay_raises_fixture_missing() -> None:
    with pytest.raises(FixtureMissing):
        MasterSources().fetch(resource="nse")


def test_parse_sec_maps_arca_spellings() -> None:
    import json

    doc = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1, "Fund A", "SPYA", "NYSE Arca"], [2, "Fund B", "QQQB", "NYSEARCA"],
                 [3, "Odd Co", "ODD", "Weird"]],
    }  # fmt: skip
    rows = {r.symbol: r.exchange for r in parse_sec(json.dumps(doc))}
    assert rows == {"SPYA": "ARCA", "QQQB": "ARCA", "ODD": "US"}


def test_amfi_fetch_follows_302() -> None:
    moved = "https://portal.amfiindia.com/spages/NAVAll-moved.txt"

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == URLS["amfi"]:
            return httpx.Response(302, headers={"Location": moved})
        return (
            httpx.Response(200, text="navall body")
            if str(req.url) == moved
            else httpx.Response(500)
        )

    assert MasterSources(client(handler)).fetch(resource="amfi").data == "navall body"


def test_sec_fetch_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/x"})

    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    with pytest.raises(SourceUnavailable, match="302"):
        MasterSources(client(handler)).fetch(resource="sec")
    assert seen == [URLS["sec"]]  # the contact User-Agent never leaves sec.gov


TODAY = date(2026, 10, 13)
UDIFF_HEAD = text("bse_scrips.csv").splitlines()[0]


def bse_url(d: date) -> str:
    return URLS["bse"].format(d=d.strftime("%Y%m%d"))


def test_bse_master_walks_back_over_404s() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return (
            httpx.Response(200, text=text("bse_scrips.csv"))
            if len(seen) == 4
            else httpx.Response(404)
        )

    got = MasterSources(client(handler), today=TODAY).fetch(resource="bse").data
    assert got == text("bse_scrips.csv")
    assert seen == [bse_url(date(2026, 10, 13 - i)) for i in range(4)]


def test_bse_master_html_200_counts_as_a_miss_and_walk_back_continues() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == bse_url(TODAY):
            return httpx.Response(200, text="<!DOCTYPE html><html></html>")
        return httpx.Response(200, text=text("bse_scrips.csv"))

    got = MasterSources(client(handler), today=TODAY).fetch(resource="bse").data
    assert got.startswith("TradDt")


def test_bse_master_gives_up_after_ten_days() -> None:
    n = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(404)

    with pytest.raises(SourceUnavailable, match="no BSE bhavcopy in the last 10 days"):
        MasterSources(client(handler), today=TODAY).fetch(resource="bse")
    assert n == 10
