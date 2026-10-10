import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.fundamentals_in import (
    IndiaFundamentals,
    parse_results_xbrl,
    parse_shareholding,
    prefer_consolidated,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import SourceUnavailable
from nivesh_core.pii_scan import scan_paths
from tests.market_fx import india_six_years, india_xbrl

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
FILED = date(2026, 1, 20)
BANK = ("gnpa_pct", "nnpa_pct", "nim_pct", "casa_pct", "car_pct")


def results(name: str = "india_results.xbrl") -> dict[tuple[str, str, date], Decimal]:
    f = parse_results_xbrl((FX / name).read_text(), FILED)
    return {(r.item, r.period_type, r.period_end): r.value for r in f.rows}


def client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_xbrl_results_parsed_to_standard_items_with_period_end_and_filed_at() -> None:
    f = parse_results_xbrl((FX / "india_results.xbrl").read_text(), FILED)
    q = {r.item: r for r in f.rows if r.period_type == "Q"}
    assert q["revenue"].period_end == date(2025, 12, 31) and q["revenue"].filed_at == FILED
    assert q["revenue"].value == Decimal("243575000.00")  # 2435.75 lakh
    assert q["eps"].value == Decimal("22.61") and q["net_income"].currency == "INR"
    assert {"operating_income", "net_income", "share_capital"} <= set(q)


def test_annual_and_quarterly_contexts_split_by_duration() -> None:
    r = results()
    assert r[("revenue", "A", date(2025, 3, 31))] == Decimal("930010000.00")
    assert ("revenue", "Q", date(2025, 12, 31)) in r
    f = parse_results_xbrl((FX / "india_results.xbrl").read_text(), FILED)
    assert f.skipped == ["revenue 2025-04-01..2025-12-31"]  # nine-month YTD, not guessed


def test_segment_context_ignored() -> None:
    assert results()[("revenue", "Q", date(2025, 12, 31))] != Decimal("99900000.00")


def test_bank_results_capture_gnpa_nnpa_nim_casa_car() -> None:
    r = results("india_bank_results.xbrl")
    end = date(2025, 12, 31)
    assert [r[(i, "Q", end)] for i in BANK] == [
        Decimal("1.24"),
        Decimal("0.33"),
        Decimal("3.45"),
        Decimal("38.20"),
        Decimal("18.90"),
    ]
    assert r[("revenue", "Q", end)] == Decimal("71005000000.00")  # crores scaled


def test_non_bank_has_no_bank_fields_and_no_zero_fill() -> None:
    assert not any(k[0] in BANK for k in results())


def test_missing_bank_field_is_absent_not_zero() -> None:
    doc = india_xbrl({"Q": ("2025-10-01", "2025-12-31")}, [("PercentageOfGrossNpa", "Q", "2.1")])
    items = {r.item for r in parse_results_xbrl(doc, FILED).rows}
    assert items == {"gnpa_pct"}


def test_standalone_vs_consolidated_prefers_consolidated_and_records_basis() -> None:
    p = {"Q": ("2025-10-01", "2025-12-31")}
    sa = parse_results_xbrl(
        india_xbrl(p, [("RevenueFromOperations", "Q", "10")], "Standalone"), FILED
    )
    co = parse_results_xbrl(india_xbrl(p, [("RevenueFromOperations", "Q", "12")]), FILED)
    only = parse_results_xbrl(
        india_xbrl(
            {"Q": ("2025-07-01", "2025-09-30")}, [("RevenueFromOperations", "Q", "9")], "Standalone"
        ),
        FILED,
    )
    rows, basis = prefer_consolidated([sa, co, only])
    assert sorted(r.value for r in rows) == [Decimal(9), Decimal(12)]
    assert basis == {date(2025, 12, 31): "consolidated", date(2025, 9, 30): "standalone"}


def test_values_in_lakhs_or_crores_scaled_by_unit_attribute() -> None:
    p = {"Q": ("2025-10-01", "2025-12-31")}
    lakh = parse_results_xbrl(india_xbrl(p, [("Equity", "Q", "1.5")], unit="INRLakhs"), FILED)
    crore = parse_results_xbrl(india_xbrl(p, [("Equity", "Q", "1.5")], unit="INRCrores"), FILED)
    plain = parse_results_xbrl(india_xbrl(p, [("Equity", "Q", "1.5")]), FILED)
    assert [x.rows[0].value for x in (lakh, crore, plain)] == [
        Decimal("150000.0"),
        Decimal("15000000.0"),
        Decimal("1.5"),
    ]


def test_shareholding_json_gives_quarterly_promoter_and_pledge_pct() -> None:
    rows = parse_shareholding(json.loads((FX / "india_shareholding.json").read_text()))
    assert [r.period_end for r in rows] == [
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    ]
    assert rows[2].promoter_pledged_pct == Decimal("1.25") and rows[0].promoter_pledged_pct is None
    assert rows[2].filed_at == date(2026, 1, 19) and rows[2].promoter_pct == Decimal("50.07")


def test_pledge_pct_over_100_rejected() -> None:
    bad = [{"quarter_end": "2025-12-31", "filed_at": "2026-01-19", "promoter_pledged_pct": "101"}]
    with pytest.raises(DataQualityError, match="0..100"):
        parse_shareholding(bad)
    with pytest.raises(DataQualityError, match="bad row"):
        parse_shareholding([{"quarter_end": "x"}])
    with pytest.raises(DataQualityError, match="expected list"):
        parse_shareholding({})


def test_six_year_synthetic_set_meets_depth_targets() -> None:
    rows = [r for x, filed in india_six_years() for r in parse_results_xbrl(x, filed).rows]
    annual = {r.period_end for r in rows if r.period_type == "A"}
    quarterly = {r.period_end for r in rows if r.period_type == "Q"}
    assert len(annual) >= 5 and len(quarterly) >= 12


def test_doctype_or_entity_in_xbrl_rejected() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(DataQualityError, match="declarations"):
        parse_results_xbrl(bomb, FILED)


def test_malformed_xml_raises_data_quality_error() -> None:
    with pytest.raises(DataQualityError, match="malformed"):
        parse_results_xbrl("<xbrl><unclosed></xbrl>", FILED)
    bad_ctx = india_xbrl({"Q": ("2025-10-01", "nope")}, [])
    with pytest.raises(DataQualityError, match="bad period"):
        parse_results_xbrl(bad_ctx, FILED)


def test_fetch_resources_return_json_native_data() -> None:
    xml = (FX / "india_results.xbrl").read_text()
    share = json.loads((FX / "india_shareholding.json").read_text())

    def handler(req: httpx.Request) -> httpx.Response:
        if "ShareHolding" in req.url.path:
            return httpx.Response(200, json=share)
        if "ResultsXbrl" in req.url.path:
            listing = {
                "period_end": "2025-12-31",
                "filed_at": "2026-01-20",
                "xbrl_url": "https://example.invalid/r.xml",
            }
            return httpx.Response(200, json=[listing])
        if req.url.path.endswith("missing.xml"):
            return httpx.Response(404)
        return httpx.Response(200, text=xml)

    f = IndiaFundamentals(client(handler))
    got = f.fetch(
        resource="results_xbrl", url="https://example.invalid/r.xml", filed_at="2026-01-20"
    )
    assert got.data == {"filed_at": "2026-01-20", "xml": xml} and got.source == "bse_xbrl"
    assert f.fetch(resource="shareholding", scrip="500325").data == share
    assert f.fetch(resource="results_index", scrip="500325").data[0]["filed_at"] == "2026-01-20"
    with pytest.raises(SourceUnavailable):
        f.fetch(resource="results_xbrl", url="https://example.invalid/missing.xml", filed_at="x")
    with pytest.raises(ValueError, match="unknown resource"):
        f.fetch(resource="nope")


def test_fetch_rejects_entity_payload_and_default_client_offline() -> None:
    f = IndiaFundamentals(client(lambda r: httpx.Response(200, text="<!ENTITY x 'y'><a/>")))
    with pytest.raises(DataQualityError):
        f.fetch(resource="results_xbrl", url="https://example.invalid/r.xml", filed_at="x")
    with pytest.raises(SourceUnavailable):
        IndiaFundamentals(client(lambda r: httpx.Response(404))).fetch(
            resource="shareholding", scrip="1"
        )
    with pytest.raises(FixtureMissing):
        IndiaFundamentals().fetch(resource="shareholding", scrip="1")


def test_india_fundamentals_fixtures_scan_clean_of_pii() -> None:
    names = ("india_results.xbrl", "india_bank_results.xbrl", "india_shareholding.json")
    assert scan_paths([FX / n for n in names]) == []
