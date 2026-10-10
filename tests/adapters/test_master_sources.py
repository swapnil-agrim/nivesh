from pathlib import Path

import httpx
import pytest

from nivesh_adapters.master_sources import (
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


def test_parse_bse_scrips_maps_code_symbol_name_isin_industry() -> None:
    rows = {r.symbol: r for r in parse_bse(text("bse_scrips.csv"))}
    assert rows["RELIANCE"].bse_code == "500325" and rows["RELIANCE"].industry == "Refineries"
    assert rows["RELIANCE"].isin == "INE002A01018" and rows["RELIANCE"].exchange == "BSE"
    assert "DELIST" not in rows


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
