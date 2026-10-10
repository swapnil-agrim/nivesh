import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from nivesh_adapters.master_sources import parse_amfi
from nivesh_adapters.nav import AmfiNavAll, MfapiClient, parse_mfapi, parse_navall
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import RateLimited, SourceUnavailable
from tests.mf_fx import FX, MON, Net, mfapi_doc, navall_text

D = Decimal
TODAY = date(2026, 2, 1)


def doc(name: str) -> object:
    return json.loads((FX / name).read_text())


def test_parse_mfapi_dates_and_navs_are_exact_decimals() -> None:
    pts = parse_mfapi(doc("mfapi_scheme.json"), today=TODAY)
    assert pts[0].date == date(2026, 1, 5) and pts[0].nav == D("45.1234")
    assert pts[-1].date == date(2026, 1, 12) and pts[-1].nav == D("46.5000")
    assert {p.source for p in pts} == {"mfapi"}
    assert all(isinstance(p.nav, Decimal) for p in pts)


def test_parse_mfapi_sorts_newest_first_input() -> None:
    pts = parse_mfapi(mfapi_doc({MON: "10", date(2026, 1, 6): "11", date(2026, 1, 7): "12"}), TODAY)
    assert [p.date for p in pts] == sorted(p.date for p in pts)
    shuffled = mfapi_doc({MON: "10", date(2026, 1, 6): "11"})
    shuffled["data"].reverse()
    assert [p.date.day for p in parse_mfapi(shuffled, TODAY)] == [5, 6]


@pytest.mark.parametrize(
    "rows",
    [
        [{"date": "09-01-2026", "nav": "0"}],
        [{"date": "09-01-2026", "nav": "-3.5"}],
        [{"date": "09-01-2026", "nav": "abc"}],
        [{"date": "2026-01-09", "nav": "10"}],
        [{"date": "31-02-2026", "nav": "10"}],
        [{"date": "09-03-2026", "nav": "10"}],  # after TODAY
        [{"date": "09-01-2026", "nav": "10"}, {"date": "09-01-2026", "nav": "11"}],
        [{"nav": "10"}],
    ],
)
def test_parse_mfapi_rejects_non_positive_nav_bad_date_future_date_duplicate(
    rows: list[dict[str, str]],
) -> None:
    with pytest.raises(DataQualityError):
        parse_mfapi({"meta": {}, "data": rows}, TODAY)


@pytest.mark.parametrize("bad", [None, [], {"meta": {}}, {"data": "x"}])
def test_parse_mfapi_rejects_wrong_shape(bad: object) -> None:
    with pytest.raises(DataQualityError):
        parse_mfapi(bad, TODAY)


def test_parse_mfapi_bad_fixture_and_empty_history() -> None:
    with pytest.raises(DataQualityError):
        parse_mfapi(doc("mfapi_scheme_bad.json"), TODAY)
    assert parse_mfapi({"meta": {}, "data": []}, TODAY) == []


def test_parse_navall_reads_nav_and_dmy_mon_date() -> None:
    got = parse_navall((FX / "navall.txt").read_text(), TODAY)
    assert set(got.points) == {"100001", "100002"}
    p = got.points["100001"]
    assert (p.date, p.nav, p.source) == (date(2026, 1, 12), D("46.5000"), "amfi_navall")


def test_parse_navall_skips_header_and_rejects_malformed_row() -> None:
    with pytest.raises(DataQualityError, match="malformed"):
        parse_navall((FX / "navall_bad.txt").read_text(), TODAY)
    for bad in (
        "100001;a;b;name;10.5;99-Zzz-2026",
        "100001;a;b;name;-4;12-Jan-2026",
        "100001;a;b;name;xx;12-Jan-2026",
        "100001;a;b;name;10;12-Jan-2027",
        "100001;a;b;name;10",
    ):
        with pytest.raises(DataQualityError):
            parse_navall(bad, TODAY)
    twice = "100001;a;b;n;10;12-Jan-2026\n100001;a;b;n;11;12-Jan-2026"
    with pytest.raises(DataQualityError, match="duplicate"):
        parse_navall(twice, TODAY)


def test_parse_navall_skips_and_counts_na_nav_rows() -> None:
    got = parse_navall((FX / "navall.txt").read_text(), TODAY)
    assert got.skipped_na == 1 and "100003" not in got.points


def test_parse_amfi_master_behaviour_unchanged_and_agrees_on_codes_and_isins() -> None:
    text = (FX / "navall.txt").read_text()
    master = parse_amfi(text)  # the E4 parser keeps codes and ISINs, drops NAV and date
    nav = parse_navall(text, TODAY)
    codes = {r.amfi_code for r in master}
    assert codes == {"100001", "100002", "100003"}
    assert codes - {"100003"} == set(nav.points)  # the N.A. NAV row has no point
    assert {r.isin for r in master if r.amfi_code == "100001"} == {
        "INF000A01011", "INF000A01029",
    }  # fmt: skip
    assert all(r.asset_class == "mf" and r.exchange == "AMFI" for r in master)


def test_mfapi_client_issues_only_get_to_mf_code_path() -> None:
    net = Net(mfapi={"100001": mfapi_doc({MON: "10"})})
    res = MfapiClient(net.client()).fetch(resource="nav", amfi_code="100001")
    assert res.source == "mfapi" and res.data["data"][0]["nav"] == "10"
    assert [(r.method, r.url.host, r.url.path) for r in net.requests] == [
        ("GET", "api.mfapi.in", "/mf/100001")
    ]


def test_navall_client_issues_only_get() -> None:
    net = Net(navall=(FX / "navall.txt").read_text())
    res = AmfiNavAll(net.client()).fetch(resource="navall")
    assert res.source == "amfi_navall" and "Example Bluechip" in res.data
    assert [r.method for r in net.requests] == ["GET"]


def test_adapters_map_http_status_to_typed_errors() -> None:
    n = Net(mfapi={})
    with pytest.raises(SourceUnavailable):  # 404: unknown scheme
        MfapiClient(n.client()).fetch(resource="nav", amfi_code="1")
    n.mfapi_status = 429
    with pytest.raises(RateLimited):
        MfapiClient(n.client()).fetch(resource="nav", amfi_code="1")
    n.mfapi_status = 503
    with pytest.raises(SourceUnavailable):
        MfapiClient(n.client()).fetch(resource="nav", amfi_code="1")
    n.navall_status = 503
    with pytest.raises(SourceUnavailable):
        AmfiNavAll(n.client()).fetch(resource="navall")


def test_amfi_code_must_be_digits_before_any_request() -> None:
    net = Net()
    with pytest.raises(DataQualityError):
        MfapiClient(net.client()).fetch(resource="nav", amfi_code="../x")
    assert net.requests == []


def test_default_client_in_ci_raises_fixture_missing() -> None:
    with pytest.raises(FixtureMissing):
        MfapiClient().fetch(resource="nav", amfi_code="100001")
    with pytest.raises(FixtureMissing):
        AmfiNavAll().fetch(resource="navall")


def test_bad_payload_is_rejected_by_validate_before_caching() -> None:
    net = Net(mfapi={"100001": doc("mfapi_scheme_bad.json")})
    with pytest.raises(DataQualityError):
        MfapiClient(net.client()).fetch(resource="nav", amfi_code="100001")
    net2 = Net(navall=(FX / "navall_bad.txt").read_text())
    with pytest.raises(DataQualityError):
        AmfiNavAll(net2.client()).fetch(resource="navall")


def test_nav_adapters_have_no_write_methods() -> None:
    for cls in (MfapiClient, AmfiNavAll):
        public = [n for n in dir(cls) if not n.startswith("_")]
        assert sorted(public) == ["fetch", "name", "source", "validate"], public
    assert issubclass(httpx.Client, object)
    assert navall_text({"1": ("1", MON)}).startswith("Scheme Code")
