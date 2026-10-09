import json
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.investright import InvestRightClient
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import InvestRightError, SessionExpired
from nivesh_core.pii_scan import scan_paths
from tests import pii_values as pv

FX = Path(__file__).resolve().parents[1] / "fixtures" / "investright"
TOKEN, KEY = pv.access_token(), pv.api_key_value()
BY_PATH = {
    "/oapi/v1/portfolio/holdings": "holdings",
    "/oapi/v1/cumulative-positions": "positions",
    "/oapi/v1/user/margins": "margins",
    "/oapi/v1/fetch-ltp": "ltp",
}


def make(
    status: int = 200, fixture: str | None = None, body: str | None = None
) -> tuple[InvestRightClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if body is not None:
            return httpx.Response(status, text=body)
        text = (FX / f"{fixture or BY_PATH[req.url.path]}.json").read_text()
        return httpx.Response(status, text=text)

    client = InvestRightClient(
        "https://broker.test",
        KEY,
        TOKEN,
        "nivesh-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, seen


def test_holdings_request_uses_raw_authorization_header_user_agent_and_api_key_query() -> None:
    c, seen = make()
    c.holdings()
    (req,) = seen
    assert req.method == "GET" and req.url.path == "/oapi/v1/portfolio/holdings"
    assert req.headers["authorization"] == TOKEN
    assert not req.headers["authorization"].startswith("Bear" + "er")
    assert req.headers["user-agent"] == "nivesh-test"
    assert req.url.params["api_key"] == KEY


def test_holdings_returns_adapter_result_with_rows_and_provenance() -> None:
    c, _ = make()
    res = c.holdings()
    assert res.source == "investright" and res.as_of.tzinfo is not None
    assert len(res.data) == 4 and res.data[0]["isin"] == "INE000A01010"


def test_positions_rows_unwrapped_from_data_net() -> None:
    c, _ = make()
    rows = c.positions().data
    assert [r["security_id"] for r in rows] == ["RELI", "CLOSEONLY"]


def test_margins_returns_funds_payload() -> None:
    c, seen = make()
    assert c.margins().data["available_cash"] == "1,00,000.00"
    assert seen[0].url.path == "/oapi/v1/user/margins"


def test_fetch_ltp_sends_instruments_and_returns_prices() -> None:
    c, seen = make()
    res = c.fetch_ltp([("NSE", "RELI"), ("NSE", "CLOSEONLY")])
    (req,) = seen
    assert req.method == "PUT" and req.url.path == "/oapi/v1/fetch-ltp"
    assert json.loads(req.content) == {
        "instruments": [
            {"exchange": "NSE", "security_id": "RELI"},
            {"exchange": "NSE", "security_id": "CLOSEONLY"},
        ]
    }
    assert req.headers["authorization"] == TOKEN
    assert res.data[0]["ltp"] == "2,505.75"


@pytest.mark.parametrize("code", [401, 403])
def test_http_auth_failure_raises_session_expired_mentioning_static_ip(code: int) -> None:
    c, _ = make(status=code, body="{}")
    with pytest.raises(SessionExpired, match="static IP"):
        c.holdings()


def test_session_expired_message_does_not_contain_the_token() -> None:
    c, _ = make(status=401, body="{}")
    with pytest.raises(SessionExpired) as ei:
        c.holdings()
    assert TOKEN not in str(ei.value) and KEY not in str(ei.value)


def test_status_not_success_raises_investright_error_with_message_and_code() -> None:
    c, _ = make(fixture="error_60014")
    with pytest.raises(InvestRightError, match="Static IP mismatch") as ei:
        c.holdings()
    assert ei.value.code == 60014


def test_non_json_body_raises_investright_error() -> None:
    c, _ = make(body="<html>oops</html>")
    with pytest.raises(InvestRightError, match="not JSON"):
        c.holdings()


def test_http_error_with_success_looking_body_still_fails() -> None:
    c, _ = make(status=500, body='{"status": "success"}')
    with pytest.raises(InvestRightError, match="500"):
        c.holdings()


def test_unexpected_data_shape_raises_data_quality_error() -> None:
    c, _ = make(body='{"status": "success", "data": "nope"}')
    with pytest.raises(DataQualityError):
        c.holdings()
    c2, _ = make(body='{"status": "success", "data": {"net": 3}}')
    with pytest.raises(DataQualityError):
        c2.positions()


def test_holdings_accepts_dict_wrapper() -> None:
    c, _ = make(body='{"status": "success", "data": {"holdings": [{"quantity": "1"}]}}')
    assert c.holdings().data == [{"quantity": "1"}]


def test_negative_quantity_row_fails_validation_with_data_quality_error() -> None:
    c, _ = make(body='{"status": "success", "data": [{"isin": "X", "quantity": "-1"}]}')
    with pytest.raises(DataQualityError, match="quantity"):
        c.holdings()


def test_default_client_comes_from_recorder_and_unrecorded_call_fails_offline() -> None:
    c = InvestRightClient("https://broker.test", KEY, TOKEN, "nivesh-test")
    with pytest.raises(FixtureMissing):
        c.holdings()


def test_investright_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths([FX]) == []
