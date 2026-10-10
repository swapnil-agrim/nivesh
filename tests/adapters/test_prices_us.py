from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.prices_in import parse_yahoo
from nivesh_adapters.prices_us import UsPrices, parse_stooq, stooq_symbol, yahoo_symbol
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from tests.market_fx import yahoo_chart

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"


def client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_us_yahoo_chart_bars_source_recorded() -> None:
    seen: list[httpx.Request] = []
    payload = yahoo_chart([170.0, 171.0], date(2024, 4, 1), gmtoffset=-14400)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=payload)

    data = UsPrices(client(handler)).fetch(
        resource="yahoo", symbol="BRK.B", start="2024-04-01", end="2024-04-02"
    )
    assert "BRK-B" in str(seen[0].url)
    bars, _ = parse_yahoo(data.data, 5)
    assert [(b.date, b.close, b.source) for b in bars] == [
        (date(2024, 4, 1), Decimal("170.0"), "yahoo"),
        (date(2024, 4, 2), Decimal("171.0"), "yahoo"),
    ]


def test_stooq_csv_parsed_as_secondary_source() -> None:
    text = (FX / "stooq_aapl.csv").read_text()
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=text)

    data = UsPrices(client(handler)).fetch(
        resource="stooq", symbol="AAPL", start="2024-03-27", end="2024-04-02"
    )
    assert seen[0].url.params["s"] == "aapl.us" and seen[0].url.params["d1"] == "20240327"
    bars = parse_stooq(data.data, 9)
    assert len(bars) == 4 and bars[0].source == "stooq" and bars[0].close == Decimal("173.31")
    assert bars[0].volume == 60000


def test_us_index_symbols_caret_prefix_mapped() -> None:
    assert yahoo_symbol("^GSPC") == "^GSPC" and stooq_symbol("^GSPC") == "^spx"
    assert stooq_symbol("^UNKNOWN") is None
    got = UsPrices(client(lambda r: httpx.Response(500))).fetch(
        resource="stooq", symbol="^UNKNOWN", start="2024-01-01", end="2024-01-02"
    )
    assert got.data == ""


def test_stooq_no_data_and_404_are_empty() -> None:
    assert parse_stooq("No data", 1) == [] and parse_stooq("", 1) == []
    got = UsPrices(client(lambda r: httpx.Response(404))).fetch(
        resource="stooq", symbol="AAPL", start="2024-01-01", end="2024-01-02"
    )
    assert got.data == ""


@pytest.mark.parametrize(
    "body, why",
    [
        ("Date,Open,High,Low,Close,Volume\n2024-01-02,1,2,1,,5\n", "missing close"),
        ("Date,Open,High,Low,Close,Volume\n2024-01-02,1,1,2,1,5\n", "high below low"),
        ("Date,Open\n2024-01-02,1\n", "missing column"),
        ("Date,Open,High,Low,Close,Volume\n02/01/2024,1,2,1,1,5\n", "bad date"),
    ],
)
def test_stooq_malformed_rows_raise(body: str, why: str) -> None:
    with pytest.raises(DataQualityError, match=why):
        parse_stooq(body, 1)
    with pytest.raises(DataQualityError):  # validation runs before anything is cached
        UsPrices(client(lambda r: httpx.Response(200, text=body))).fetch(
            resource="stooq", symbol="AAPL", start="2024-01-01", end="2024-01-02"
        )


def test_unknown_resource_and_default_client_offline() -> None:
    with pytest.raises(ValueError, match="unknown resource"):
        UsPrices(client(lambda r: httpx.Response(200))).fetch(
            resource="x", symbol="A", start="a", end="b"
        )
    with pytest.raises(FixtureMissing):
        UsPrices().fetch(resource="stooq", symbol="AAPL", start="2024-01-01", end="2024-01-02")
