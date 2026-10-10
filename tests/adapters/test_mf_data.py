import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from nivesh_adapters.mf_data import MfMetaClient, parse_meta
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import RateLimited, SourceUnavailable
from tests.mf_fx import FX, Net

D = Decimal
TODAY = date(2026, 2, 1)


def doc(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = json.loads((FX / "meta.json").read_text())
    base.update(over)
    return base


def test_parse_meta_maps_category_plan_option_ter_aum_benchmark_manager_since_as_of() -> None:
    m = parse_meta(doc(), TODAY)
    assert m.amfi_code == "100001" and m.amc == "Example Mutual Fund"
    assert m.category == "Equity Scheme - Large Cap Fund"
    assert (m.plan, m.option) == ("regular", "growth")
    assert m.expense_ratio == D("1.55") and m.aum_crore == D(
        "1250.50"
    )  # crore, as the source states it
    assert (m.benchmark, m.manager) == ("Example Equity Index", "Example Manager")
    assert (m.manager_since, m.as_of, m.source) == (date(2022, 4, 1), date(2026, 1, 12), "mf_meta")


def test_missing_ter_and_aum_are_null() -> None:
    d = doc()
    del d["expense_ratio"], d["aum_crore"], d["manager_since"], d["benchmark"]
    m = parse_meta(d, TODAY)
    assert m.expense_ratio is None and m.aum_crore is None and m.manager_since is None
    assert m.benchmark is None
    blank = parse_meta(doc(expense_ratio="", aum_crore=None, amc=None), TODAY)
    assert blank.expense_ratio is None and blank.aum_crore is None and blank.amc is None


def test_as_of_defaults_to_the_fetch_date() -> None:
    d = doc()
    del d["as_of"]
    assert parse_meta(d, TODAY).as_of == TODAY


@pytest.mark.parametrize(
    "over",
    [
        {"expense_ratio": "-0.1"},
        {"expense_ratio": "9.5"},
        {"expense_ratio": "abc"},
        {"aum_crore": "-4"},
        {"manager_since": "not-a-date"},
        {"as_of": "2027-12-31"},
        {"scheme_name": ""},
        {"amfi_code": "12x"},
    ],
)
def test_ter_out_of_range_and_negative_aum_raise_data_quality_error(over: dict[str, Any]) -> None:
    with pytest.raises(DataQualityError):
        parse_meta(doc(**over), TODAY)


def test_wrong_shape_rejected() -> None:
    for bad in (None, [], "x", {}):
        with pytest.raises(DataQualityError):
            parse_meta(bad, TODAY)


def test_plan_option_normalised_from_scheme_name_not_from_holding_plan() -> None:
    d = doc(scheme_name="Example Bluechip Fund - Direct Plan - IDCW Payout", plan="regular")
    m = parse_meta(d, TODAY)
    assert (m.plan, m.option) == ("direct", "idcw")
    assert parse_meta(doc(scheme_name="Example Fund"), TODAY).plan is None


def test_meta_client_get_only_and_error_mapping() -> None:
    net = Net()
    net.mfapi = {}
    client = MfMetaClient(net.client())
    with pytest.raises(SourceUnavailable):  # Net answers 500 for unknown hosts
        client.fetch(resource="meta", amfi_code="100001")
    assert [r.method for r in net.requests] == ["GET"]
    with pytest.raises(DataQualityError):
        client.fetch(resource="meta", amfi_code="../x")


def test_meta_client_fetch_validates_and_has_no_write_methods() -> None:
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(doc()))

    ok = MfMetaClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert ok.fetch(resource="meta", amfi_code="100001").source == "mf_meta"
    bad = MfMetaClient(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    )
    with pytest.raises(DataQualityError):
        bad.fetch(resource="meta", amfi_code="100001")
    limited = MfMetaClient(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    )
    with pytest.raises(RateLimited):
        limited.fetch(resource="meta", amfi_code="100001")
    assert sorted(n for n in dir(MfMetaClient) if not n.startswith("_")) == [
        "fetch", "name", "source", "validate",
    ]  # fmt: skip
    with pytest.raises(FixtureMissing):
        MfMetaClient().fetch(resource="meta", amfi_code="100001")


# ---- monthly holdings ------------------------------------------------------------------------
from nivesh_adapters.mf_data import MfHoldingsClient, parse_holdings  # noqa: E402
from nivesh_core.errors import SecretNotFound  # noqa: E402
from tests import pii_values as pv  # noqa: E402


def hdoc(name: str = "holdings_12m.json") -> dict[str, Any]:
    out: dict[str, Any] = json.loads((FX / name).read_text())
    return out


def test_parse_holdings_isin_and_weight_for_each_month() -> None:
    got = parse_holdings(hdoc(), "fixture", "100001")
    assert len(got) == 12 and min(got) == date(2025, 1, 31) and max(got) == date(2025, 12, 31)
    dec = {r.isin: r for r in got[date(2025, 12, 31)]}
    assert dec["INE000A01010"].weight_pct == D("30.50") and dec["INE000A01010"].kind == "equity"
    assert all(r.source == "mf_holdings" for rows in got.values() for r in rows)


def test_cash_and_derivative_lines_are_other_with_label_key() -> None:
    rows = parse_holdings(hdoc(), "fixture", "100001")[date(2025, 12, 31)]
    other = {r.isin: r for r in rows if r.kind == "other"}
    assert set(other) == {"OTHER:CASH_AND_EQUIVALENTS", "OTHER:INDEX_FUTURES"}
    assert other["OTHER:INDEX_FUTURES"].weight_pct == D("4.60")


def test_amc_flat_shape_parses_to_the_same_rows() -> None:
    nested = parse_holdings(hdoc(), "mfdata", "100001")
    flat_rows = []
    for m in hdoc()["months"]:
        y, mo, d = m["month_end"].split("-")
        for h in m["holdings"]:
            flat_rows.append(
                {
                    "as_on": f"{d}-{mo}-{y}", "isin": h.get("isin"), "label": h.get("label"),
                    "pct": h["weight_pct"], "type": h["asset_type"].title(),
                }
            )  # fmt: skip
    amc = parse_holdings({"scheme": "100001", "rows": flat_rows}, "amc", "100001")
    assert amc == nested


def test_weights_sum_over_101_percent_raises_data_quality_error() -> None:
    with pytest.raises(DataQualityError, match="101"):
        parse_holdings(hdoc("holdings_bad.json"), "fixture", "100001")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["holdings"][0].update(isin="BAD"),
        lambda m: m["holdings"][0].update(weight_pct="-1"),
        lambda m: m["holdings"][0].update(weight_pct="101"),
        lambda m: m["holdings"][0].update(weight_pct="abc"),
        lambda m: m.update(month_end="2025-12-15"),
        lambda m: m.update(month_end="12/2025"),
        lambda m: m["holdings"].append(dict(m["holdings"][0])),
        lambda m: m["holdings"][-1].pop("label"),
        lambda m: m["holdings"][0].pop("asset_type"),
    ],
)
def test_bad_isin_weight_or_month_end_rejected(mutate: Any) -> None:
    d = hdoc()
    d["months"] = d["months"][-1:]
    mutate(d["months"][0])
    with pytest.raises(DataQualityError):
        parse_holdings(d, "fixture", "100001")


def test_wrong_shape_wrong_code_and_unknown_source_rejected() -> None:
    for bad in (None, [], {"months": "x"}, {"amfi_code": "100001"}):
        with pytest.raises(DataQualityError):
            parse_holdings(bad, "fixture", "100001")
    with pytest.raises(DataQualityError, match="amfi_code"):
        parse_holdings(hdoc(), "fixture", "100002")
    with pytest.raises(DataQualityError, match="source"):
        parse_holdings(hdoc(), "nope", "100001")
    with pytest.raises(DataQualityError):
        parse_holdings({"scheme": "100001", "rows": [{"as_on": "x"}]}, "amc", "100001")


def test_holdings_client_get_only_no_write_methods_and_no_key_for_fixture_source() -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=json.dumps(hdoc()))

    c = httpx.Client(transport=httpx.MockTransport(handler))
    res = MfHoldingsClient(c, source="fixture").fetch(resource="holdings", amfi_code="100001")
    assert res.source == "mf_holdings" and len(res.data["months"]) == 12
    assert [r.method for r in seen] == ["GET"] and seen[0].url.path.endswith("/100001")
    assert sorted(n for n in dir(MfHoldingsClient) if not n.startswith("_")) == [
        "fetch", "name", "source", "validate",
    ]  # fmt: skip
    with pytest.raises(DataQualityError):
        MfHoldingsClient(c, source="fixture").fetch(resource="holdings", amfi_code="x/y")


def test_holdings_key_is_resolved_from_the_reference_and_sent_only_as_a_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    monkeypatch.setenv("MFDATA_API_KEY", pv.mf_source_ref_value())
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=json.dumps(hdoc()))

    c = httpx.Client(transport=httpx.MockTransport(handler))
    client = MfHoldingsClient(c, source="mfdata", key_ref="ref:MFDATA_API_KEY")
    client.fetch(resource="holdings", amfi_code="100001")
    assert pv.mf_source_ref_value() not in str(seen[0].url)
    assert pv.mf_source_ref_value() in seen[0].headers.values()
    monkeypatch.delenv("MFDATA_API_KEY")
    with pytest.raises(SecretNotFound, match="MFDATA_API_KEY"):
        client.fetch(resource="holdings", amfi_code="100001")
    with pytest.raises(Exception, match="reference"):
        MfHoldingsClient(c, source="mfdata", key_ref="plain")
