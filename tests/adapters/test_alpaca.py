import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.alpaca import AlpacaClient, BrokerReader, normalise_positions
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_paths
from nivesh_core.security_resolver import Resolved
from nivesh_mcp.base import write_methods
from tests import pii_values as pv

ROOT = Path(__file__).resolve().parents[2]
FX = ROOT / "tests" / "fixtures" / "alpaca"
DUMMY_ID, DUMMY_PW = pv.alpaca_key_value(), pv.alpaca_secret_value()
TODAY = date(2026, 1, 5)
BY_PATH = {"/v2/positions": "positions", "/v2/account": "account"}


def make(status: int = 200, body: str | None = None) -> tuple[AlpacaClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        text = body if body is not None else (FX / f"{BY_PATH[req.url.path]}.json").read_text()
        return httpx.Response(status, text=text)

    client = AlpacaClient(
        "https://broker.test",
        DUMMY_ID,
        DUMMY_PW,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, seen


def test_positions_and_account_hit_only_get_v2_paths() -> None:
    c, seen = make()
    c.positions()
    c.account()
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v2/positions"),
        ("GET", "/v2/account"),
    ]
    for r in seen:
        assert r.headers[pv.alpaca_key_header()] == DUMMY_ID
        assert r.headers[pv.alpaca_secret_header()] == DUMMY_PW
        assert not r.url.query and not r.content


def test_credentials_never_in_returned_data_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    c, _ = make()
    caplog.set_level("DEBUG")
    blob = json.dumps(c.positions().data) + json.dumps(c.account().data) + caplog.text
    assert DUMMY_ID not in blob and DUMMY_PW not in blob


def test_account_is_whitelisted_account_number_and_id_dropped() -> None:
    c, _ = make()
    data = c.account().data
    assert data == {"currency": "USD", "status": "ACTIVE", "cash": "1000.00", "equity": "5000.00"}


def test_positions_whitelist_drops_asset_id_and_account_fields() -> None:
    c, _ = make()
    rows = c.positions().data
    assert len(rows) == 7
    allowed = {
        "symbol",
        "exchange",
        "qty",
        "avg_entry_price",
        "current_price",
        "asset_class",
        "side",
    }
    assert all(set(r) <= allowed for r in rows)
    assert "asset-sample-1" not in json.dumps(rows) and "market_value" not in rows[0]


@pytest.mark.parametrize("code", [401, 403])
def test_http_401_raises_clear_error_without_echoing_headers(code: int) -> None:
    c, _ = make(status=code, body="{}")
    with pytest.raises(NiveshError, match="credentials") as ei:
        c.positions()
    assert DUMMY_ID not in str(ei.value) and DUMMY_PW not in str(ei.value)


def test_other_http_error_and_non_json_fail_clearly() -> None:
    with pytest.raises(NiveshError, match="HTTP 500"):
        make(status=500, body="{}")[0].positions()
    with pytest.raises(NiveshError, match="not JSON"):
        make(body="<html>")[0].positions()


def test_non_list_payload_is_data_quality_error() -> None:
    c, _ = make(body='{"oops": 1}')
    with pytest.raises(DataQualityError, match="expected"):
        c.positions()
    with pytest.raises(DataQualityError, match="expected"):
        make(body="[1]")[0].account()


def test_default_client_in_ci_raises_fixture_missing() -> None:
    c = AlpacaClient("https://broker.test", DUMMY_ID, DUMMY_PW)
    with pytest.raises(FixtureMissing):
        c.positions()


def test_alpaca_client_has_no_write_methods() -> None:
    assert write_methods(AlpacaClient) == []
    reader: BrokerReader = make()[0]
    assert {n for n in dir(reader) if not n.startswith("_")} >= {"positions", "account"}


class Master:
    def __init__(self, rows: dict[str, str] | None = None) -> None:
        self.rows = rows or {}

    def resolve_symbol(self, symbol: str, exchange_hint: str = "") -> Resolved | None:
        if symbol not in self.rows:
            return None
        return Resolved(
            symbol=symbol, name=f"{symbol} Corp", exchange=self.rows[symbol], asset_class="equity"
        )


def rows() -> list[dict[str, str]]:
    return make()[0].positions().data  # type: ignore[no-any-return]


def test_normalise_positions_to_usd_holdings() -> None:
    held, warnings = normalise_positions(rows(), Master(), TODAY)
    by = {h.symbol: h for h in held}
    aapl = by["AAPL"]
    assert aapl.currency == "USD" and aapl.source == "alpaca" and aapl.value_inr is None
    assert aapl.price == Decimal("190.00") and aapl.price_basis == "ltp"
    assert aapl.avg_cost == Decimal("150.25") and aapl.quantity == 10
    assert (
        aapl.asset_class == "equity" and aapl.exchange == "NASDAQ" and aapl.source_label == "Alpaca"
    )
    spyx = by["SPYX"]  # no current price: falls back to cost
    assert spyx.price == Decimal("92.10") and spyx.price_basis == "avg_cost"
    assert spyx.quantity == Decimal("4.5") and spyx.exchange == "ARCA"
    assert warnings  # something was skipped


def test_short_zero_and_unsupported_exchange_rows_skipped_with_warning() -> None:
    held, warnings = normalise_positions(rows(), Master(), TODAY)
    assert {h.symbol for h in held} == {"AAPL", "SPYX", "AMEXCO"}
    text = " | ".join(warnings)
    assert "1 short position(s)" in text and "1 non-equity position(s)" in text
    assert "1 position(s) with an unsupported symbol" in text
    assert "1 position(s) on an unsupported exchange" in text
    zero = [{"symbol": "ZERO", "exchange": "NYSE", "asset_class": "us_equity", "qty": "0",
             "avg_entry_price": "1", "current_price": "1", "side": "long"}]  # fmt: skip
    assert normalise_positions(zero, Master(), TODAY) == ([], [])


def test_master_exchange_wins_and_amex_maps_to_nyse() -> None:
    held, _ = normalise_positions(rows(), Master({"AAPL": "NYSE"}), TODAY)
    by = {h.symbol: h for h in held}
    assert by["AAPL"].exchange == "NYSE" and by["AAPL"].name == "AAPL Corp"
    assert by["AMEXCO"].exchange == "NYSE"


def test_bad_numbers_are_data_quality_errors() -> None:
    bad = [{"symbol": "AAA", "exchange": "NYSE", "asset_class": "us_equity", "qty": "x",
            "avg_entry_price": "1", "current_price": "1", "side": "long"}]  # fmt: skip
    with pytest.raises(DataQualityError):
        normalise_positions(bad, Master(), TODAY)
    no_cost = [{**bad[0], "qty": "1", "avg_entry_price": ""}]
    with pytest.raises(DataQualityError, match="avg_entry_price"):
        normalise_positions(no_cost, Master(), TODAY)


def test_fixtures_are_pii_clean() -> None:
    assert scan_paths([FX]) == []
