import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from nivesh_adapters.estimates import (
    Estimates,
    check_available,
    parse_fmp_calendar,
    parse_fmp_estimates,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_core.errors import SecretNotFound, SourceUnavailable
from tests import pii_values as pv
from tests.market_fx import fmp_calendar, fmp_estimates

D = Decimal
AS_OF = date(2026, 1, 5)


class Net:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status)
        body = fmp_calendar() if "earning_calendar" in req.url.path else fmp_estimates()
        return httpx.Response(200, json=body)


def adapter(net: Net) -> Estimates:
    return Estimates(httpx.Client(transport=httpx.MockTransport(net)))


def test_fmp_estimates_parsed_revenue_and_eps_next_fy() -> None:
    pts = parse_fmp_estimates(fmp_estimates(), AS_OF)
    assert [(p.metric, p.period, p.value) for p in pts[:2]] == [
        ("eps", "2026-09-30", D("7.45")),
        ("revenue", "2026-09-30", D("391035000000")),
    ]
    assert {p.period for p in pts} == {"2026-09-30", "2027-09-30"}  # past years are dropped


def test_fmp_bad_payloads() -> None:
    with pytest.raises(DataQualityError, match="expected list"):
        parse_fmp_estimates({"Error Message": "x"}, AS_OF)
    bad = [{"date": "2026-09-30", "estimatedEpsAvg": "x"}]
    with pytest.raises(DataQualityError, match="not a number"):
        parse_fmp_estimates(bad, AS_OF)
    with pytest.raises(DataQualityError, match="bad row"):
        parse_fmp_estimates([{"estimatedEpsAvg": 1}], AS_OF)
    assert parse_fmp_estimates([{"date": "2026-09-30"}], AS_OF) == []  # no metric: nothing to keep


def test_fmp_key_query_param_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FMP_API_KEY", pv.fmp_key())
    net = Net()
    res = adapter(net).fetch(resource="estimates", symbol="AAPL")
    assert net.requests[0].url.params["apikey"] == pv.fmp_key()
    assert pv.fmp_key() not in json.dumps(res.data) and res.data == fmp_estimates()
    with pytest.raises(SourceUnavailable) as e:
        adapter(Net(503)).fetch(resource="estimates", symbol="AAPL")
    assert pv.fmp_key() not in str(e.value)


def test_missing_fmp_key_marks_unavailable_with_reason_not_zero(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    res = check_available("US")
    assert res is not None and res.available is False and res.points == []
    assert res.reason is not None and "secrets set FMP_API_KEY" in res.reason
    with pytest.raises(SecretNotFound):
        adapter(Net()).fetch(resource="estimates", symbol="AAPL")
    monkeypatch.setenv("FMP_API_KEY", pv.fmp_key())
    assert check_available("US") is None


def test_india_security_returns_unavailable_marker_never_zero() -> None:
    res = check_available("IN")
    assert res is not None and res.available is False and res.points == []
    assert res.reason is not None and "India" in res.reason


def test_earnings_calendar_rows_become_calendar_event_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FMP_API_KEY", pv.fmp_key())
    res = adapter(Net()).fetch(resource="earnings_calendar", symbol="AAPL")
    assert parse_fmp_calendar(res.data, AS_OF) == [date(2026, 2, 1), date(2026, 5, 1)]
    with pytest.raises(DataQualityError, match="bad row"):
        parse_fmp_calendar([{"x": 1}], AS_OF)
    with pytest.raises(DataQualityError, match="expected list"):
        parse_fmp_calendar({}, AS_OF)


def test_unknown_resource() -> None:
    with pytest.raises(ValueError, match="unknown resource"):
        adapter(Net()).fetch(resource="nope")
