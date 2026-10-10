import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.macro import MacroFetch, parse_flows, parse_fred
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import SecretNotFound, SourceUnavailable
from nivesh_core.pii_scan import scan_paths
from tests import pii_values as pv

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
D = Decimal


class Net:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status)
        if "stlouisfed" in req.url.host:
            return httpx.Response(200, text=(FX / "fred_obs.json").read_text())
        return httpx.Response(200, text=(FX / "nse_fiidii.json").read_text())


def fetcher(net: Net) -> MacroFetch:
    return MacroFetch(httpx.Client(transport=httpx.MockTransport(net)))


def test_fred_observations_parsed_decimal_and_missing_dot_values_skipped() -> None:
    data = json.loads((FX / "fred_obs.json").read_text())
    obs = parse_fred(data)
    assert [(o.date, o.value) for o in obs] == [
        (date(2026, 1, 2), D("4.50")),
        (date(2026, 1, 6), D("4.55")),
        (date(2026, 1, 7), D("-0.10")),  # negative rates are valid
    ]


def test_fred_bad_payload_raises_data_quality_error() -> None:
    with pytest.raises(DataQualityError, match="observations"):
        parse_fred({"error_message": "bad"})
    with pytest.raises(DataQualityError, match="not a number"):
        parse_fred({"observations": [{"date": "2026-01-02", "value": "abc"}]})
    with pytest.raises(DataQualityError, match="bad date"):
        parse_fred({"observations": [{"date": "x", "value": "1"}]})


def test_fred_api_key_sent_as_query_param_and_never_stored_or_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", pv.fred_key())
    net = Net()
    res = fetcher(net).fetch(resource="fred", series_id="DGS10", years=3)
    req = net.requests[0]
    assert req.url.params["api_key"] == pv.fred_key() and req.url.params["series_id"] == "DGS10"
    assert "observation_start" in req.url.params and req.url.params["file_type"] == "json"
    assert pv.fred_key() not in json.dumps(res.data)


def test_missing_fred_key_raises_secret_not_found_with_hint(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(SecretNotFound, match="secrets set FRED_API_KEY"):
        fetcher(Net()).fetch(resource="fred", series_id="DGS10", years=3)


def test_http_failure_is_source_unavailable_and_key_not_in_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", pv.fred_key())
    with pytest.raises(SourceUnavailable) as e:
        fetcher(Net(503)).fetch(resource="fred", series_id="DGS10", years=3)
    assert pv.fred_key() not in str(e.value)


def test_nse_fii_dii_flows_parsed_inflow_outflow_net() -> None:
    rows = parse_flows(json.loads((FX / "nse_fiidii.json").read_text()))
    by = {r.category: r for r in rows}
    assert set(by) == {"fii", "dii"}
    assert by["fii"].day == date(2026, 1, 9) and by["fii"].net == D("-1500.50")
    assert by["dii"].inflow == D("12345.67") and by["dii"].outflow == D("11000.10")


def test_flows_bad_rows_rejected() -> None:
    with pytest.raises(DataQualityError, match="expected list"):
        parse_flows({})
    with pytest.raises(DataQualityError, match="bad row"):
        parse_flows([{"category": "FII"}])


def test_unknown_resource_and_offline_default_client() -> None:
    with pytest.raises(ValueError, match="unknown resource"):
        fetcher(Net()).fetch(resource="nope")
    with pytest.raises(FixtureMissing):
        MacroFetch().fetch(resource="nse_flows")


def test_nse_flows_fetch_returns_json_native() -> None:
    res = fetcher(Net()).fetch(resource="nse_flows")
    assert json.loads(json.dumps(res.data)) == res.data


def test_macro_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths([FX / "fred_obs.json", FX / "nse_fiidii.json"]) == []
