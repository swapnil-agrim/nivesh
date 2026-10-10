import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.prices_in import (
    IndiaPrices,
    parse_bhavcopy,
    parse_corp_actions,
    parse_indices,
    parse_yahoo,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import RateLimited, SourceUnavailable
from nivesh_core.pii_scan import scan_paths
from tests.market_fx import yahoo_chart

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
IDS = {"RELIANCE": 1, "TCS": 2, "SBIN": 3}


def text(name: str) -> str:
    return (FX / name).read_text()


def client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def zipped(body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy.csv", body)
    return buf.getvalue()


def test_nse_bhavcopy_zip_parsed_to_ohlcv_with_source_nse_bhavcopy() -> None:
    payload = zipped(text("nse_bhavcopy.csv"))
    ip = IndiaPrices(client(lambda r: httpx.Response(200, content=payload)))
    data = ip.fetch(resource="nse_bhavcopy", day="2026-01-05").data
    assert isinstance(data, str)
    parsed = parse_bhavcopy(data, IDS, key_col="TckrSymb", source="nse_bhavcopy")
    rel = next(b for b in parsed.bars if b.security_id == 1)
    assert (rel.date, rel.open, rel.high, rel.low, rel.close, rel.volume, rel.source) == (
        date(2026, 1, 5), Decimal("1300.50"), Decimal("1320.00"), Decimal("1295.25"),
        Decimal("1310.75"), 5400000, "nse_bhavcopy",
    )  # fmt: skip


def test_series_filter_keeps_eq_and_etf_drops_others() -> None:
    parsed = parse_bhavcopy(
        text("nse_bhavcopy.csv"), {**IDS, "DEBTCO": 9}, key_col="TckrSymb", source="nse_bhavcopy"
    )
    assert {b.security_id for b in parsed.bars} == {1, 2, 3}


def test_bse_bhavcopy_csv_parsed_with_source_bse_bhavcopy() -> None:
    ip = IndiaPrices(client(lambda r: httpx.Response(200, text=text("bse_bhavcopy.csv"))))
    data = ip.fetch(resource="bse_bhavcopy", day="2026-01-05").data
    parsed = parse_bhavcopy(
        data, {"500325": 1}, key_col="FinInstrmId", source="bse_bhavcopy", series=None
    )
    assert [(b.security_id, b.close, b.source) for b in parsed.bars] == [
        (1, Decimal("1310.50"), "bse_bhavcopy")
    ]
    assert parsed.skipped == 1


def test_indices_file_yields_nifty_sensex_and_india_vix_bars() -> None:
    ip = IndiaPrices(client(lambda r: httpx.Response(200, text=text("nse_indices.csv"))))
    data = ip.fetch(resource="indices", day="2026-01-05").data
    parsed = parse_indices(data, {"NIFTY 50": 10, "INDIA VIX": 11, "SENSEX": 12})
    got = {b.security_id: b for b in parsed.bars}
    assert got[10].close == Decimal("22100.25") and got[10].volume == 25000000
    assert got[11].volume is None and got[12].date == date(2026, 1, 5)
    assert parsed.skipped == 1


def test_unknown_symbol_skipped_and_counted_not_created() -> None:
    parsed = parse_bhavcopy(text("nse_bhavcopy.csv"), IDS, key_col="TckrSymb", source="nse")
    assert parsed.skipped == 1 and len(parsed.bars) == 3


def test_non_trading_day_http_404_returns_empty_not_error() -> None:
    ip = IndiaPrices(client(lambda r: httpx.Response(404)))
    assert ip.fetch(resource="nse_bhavcopy", day="2026-01-26").data == ""
    assert ip.fetch(resource="bse_bhavcopy", day="2026-01-26").data == ""
    assert ip.fetch(resource="indices", day="2026-01-26").data == ""
    assert ip.fetch(resource="corp_actions", symbol="X", start="a", end="b").data == []
    with pytest.raises(SourceUnavailable):
        ip.fetch(resource="yahoo", symbol="X.NS", start="2026-01-01", end="2026-01-05")


@pytest.mark.parametrize(
    "o, h, low, c, why",
    [
        ("10", "9", "11", "10", "high below low"),
        ("10", "12", "9", "13", "outside"),
        ("-1", "12", "9", "10", ">= 0"),
        ("10", "12", "9", "", "missing close"),
    ],
)
def test_negative_or_inverted_ohlc_raises_data_quality_error(
    o: str, h: str, low: str, c: str, why: str
) -> None:
    head = text("nse_bhavcopy.csv").splitlines()[0]
    row = f"2026-01-05,2026-01-05,CM,NSE,STK,1,ISIN,TCS,EQ,{o},{h},{low},{c},1,1,100"
    with pytest.raises(DataQualityError, match=why):
        parse_bhavcopy(f"{head}\n{row}\n", IDS, key_col="TckrSymb", source="nse")


def test_bhavcopy_missing_column_or_bad_date_raises() -> None:
    with pytest.raises(DataQualityError, match="missing column"):
        parse_bhavcopy("TckrSymb,SctySrs\nTCS,EQ\n", IDS, key_col="TckrSymb", source="nse")
    head = text("nse_bhavcopy.csv").splitlines()[0]
    row = "05/01/2026,x,CM,NSE,STK,1,ISIN,TCS,EQ,10,12,9,11,1,1,100"
    with pytest.raises(DataQualityError, match="bad date"):
        parse_bhavcopy(f"{head}\n{row}\n", IDS, key_col="TckrSymb", source="nse")


def test_indices_bad_rows_raise() -> None:
    head = text("nse_indices.csv").splitlines()[0]
    with pytest.raises(DataQualityError, match="missing column"):
        parse_indices("Index Name\nNifty 50\n", {"NIFTY 50": 1})
    with pytest.raises(DataQualityError, match="bad date"):
        parse_indices(f"{head}\nNifty 50,2026-01-05,1,2,1,1,0,0,-\n", {"NIFTY 50": 1})
    with pytest.raises(DataQualityError, match="missing close"):
        parse_indices(f"{head}\nNifty 50,05-01-2026,1,2,1,-,0,0,-\n", {"NIFTY 50": 1})


def test_yahoo_chart_fallback_parses_split_adjusted_close_and_events() -> None:
    payload = yahoo_chart(
        [100.0, 101.0, 25.5, 26.0], date(2026, 1, 5), splits=((2, 4, 1),), dividends=((3, 0.5),)
    )
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=payload)

    ip = IndiaPrices(client(handler))
    data = ip.fetch(resource="yahoo", symbol="RELIANCE.NS", start="2026-01-05", end="2026-01-08")
    assert "RELIANCE.NS" in str(seen[0].url) and seen[0].url.params["events"] == "div,splits"
    bars, acts = parse_yahoo(data.data, 7)
    assert [b.date for b in bars][0] == date(2026, 1, 5)
    assert bars[2].close == Decimal("25.5") and bars[0].source == "yahoo"
    assert [(a.kind, a.ex_date, a.ratio, a.amount) for a in acts] == [
        ("split", date(2026, 1, 7), Decimal(4), None),
        ("dividend", date(2026, 1, 8), None, Decimal("0.5")),
    ]


def test_yahoo_null_close_rows_skipped() -> None:
    payload = yahoo_chart([100.0, 101.0], date(2026, 1, 5))
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    bars, _ = parse_yahoo(payload, 1)
    assert len(bars) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"chart": {"result": None, "error": {"code": "Not Found"}}},
        {"chart": {"result": []}},
        {"nope": 1},
    ],
)
def test_yahoo_error_payload_raises_data_quality_error(payload: object) -> None:
    with pytest.raises(DataQualityError):
        parse_yahoo(payload, 1)


def test_corp_actions_json_maps_split_bonus_dividend() -> None:
    import json

    rows = json.loads(text("nse_corp_actions.json"))
    ip = IndiaPrices(client(lambda r: httpx.Response(200, json=rows)))
    data = ip.fetch(resource="corp_actions", symbol="RELIANCE", start="01-01-2026", end="x").data
    acts = parse_corp_actions(data, 1)
    assert [(a.kind, a.ex_date, a.ratio, a.amount) for a in acts] == [
        ("bonus", date(2026, 1, 5), Decimal(1), None),
        ("split", date(2026, 2, 10), Decimal(5), None),
        ("dividend", date(2026, 3, 20), None, Decimal("5.5")),
    ]
    with pytest.raises(DataQualityError, match="expected list"):
        parse_corp_actions({"data": []}, 1)


def test_http_errors_map_to_rate_limited_and_source_unavailable() -> None:
    with pytest.raises(RateLimited):
        IndiaPrices(client(lambda r: httpx.Response(429))).fetch(
            resource="indices", day="2026-01-05"
        )
    with pytest.raises(SourceUnavailable, match="503"):
        IndiaPrices(client(lambda r: httpx.Response(503))).fetch(
            resource="indices", day="2026-01-05"
        )
    with pytest.raises(ValueError, match="unknown resource"):
        IndiaPrices(client(lambda r: httpx.Response(200))).fetch(resource="nope")


def test_default_client_comes_from_recorder_and_unrecorded_call_fails_offline() -> None:
    with pytest.raises(FixtureMissing):
        IndiaPrices().fetch(resource="indices", day="2026-01-05")


def test_market_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths([FX]) == []


def test_corp_action_split_to_re_and_lone_dot_dividend() -> None:
    rows = [
        {"subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
         "exDate": "10-Feb-2026"},
        {"subject": "Dividend - Rs .", "exDate": "11-Feb-2026"},
        {"subject": "Dividend - Rs 2.50 Per Share", "exDate": "12-Feb-2026"},
    ]  # fmt: skip
    acts = parse_corp_actions(rows, 1)
    assert [(a.kind, a.ratio, a.amount) for a in acts] == [
        ("split", Decimal(10), None),
        ("dividend", None, Decimal("2.50")),
    ]


@pytest.mark.parametrize(
    "body", [b"<html>Access Denied</html>", b"", None], ids=["html", "empty-body", "empty-zip"]
)
def test_bhavcopy_bad_archive_raises_source_unavailable(body: bytes | None) -> None:
    if body is None:
        buf = io.BytesIO()
        zipfile.ZipFile(buf, "w").close()
        body = buf.getvalue()  # a valid but empty archive
    ip = IndiaPrices(client(lambda r: httpx.Response(200, content=body)))
    with pytest.raises(SourceUnavailable):
        ip.fetch(resource="nse_bhavcopy", day="2026-01-05")


def test_bhavcopy_zip_bomb_is_refused() -> None:
    from nivesh_adapters import prices_in

    payload = zipped("x" * 2000)
    ip = IndiaPrices(client(lambda r: httpx.Response(200, content=payload)))
    old = prices_in.MAX_UNZIPPED
    prices_in.MAX_UNZIPPED = 1000
    try:
        with pytest.raises(SourceUnavailable, match="too large"):
            ip.fetch(resource="nse_bhavcopy", day="2026-01-05")
    finally:
        prices_in.MAX_UNZIPPED = old
