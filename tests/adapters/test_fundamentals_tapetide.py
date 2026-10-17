"""Tapetide adapter: transport, auth, error classification, pacing. Fixtures only, no network."""

import json
import sqlite3
from contextlib import suppress
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from nivesh_adapters.cache import cached_fetch
from nivesh_adapters.fundamentals_tapetide import (
    Statements,
    TapetideFundamentals,
    _dec,
    _real,
    face_value_of,
    map_shareholding,
    map_statements,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_core.config import Ttls
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.errors import (
    MarketDataError,
    NotConfigured,
    QuotaExceeded,
    RateLimited,
    SourceUnavailable,
)
from nivesh_core.quota import Budget
from tests.conftest import MemoryKeyring
from tests.tapetide_fx import (
    Tapetide,
    adapter,
    client,
    envelope,
    inner,
    ledger,
    memory_budget,
    scheme,
    token,
)

QUARTERLY = {"resource": "financials", "symbol": "RELIANCE", "section": "quarterly"}


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring) -> None:
    """The env var wins in `get_secret`; the fake keychain keeps the real one out of every test."""
    monkeypatch.setenv("TAPETIDE_TOKEN", token())


def test_fetch_sends_post_jsonrpc_with_bearer_header_from_secret() -> None:
    srv = Tapetide()
    res = adapter(srv).fetch(**QUARTERLY)
    req, body = srv.requests[0], srv.bodies[0]
    assert req.method == "POST" and str(req.url) == "https://mcp.tapetide.com/mcp"
    assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
    assert body["params"] == {
        "name": "get_financials",
        "arguments": {"symbol": "RELIANCE", "section": "quarterly", "periods": 40},
    }
    assert req.headers["authorization"] == f"{scheme()} {token()}"
    assert "text/event-stream" in req.headers["accept"]
    assert res.source == "tapetide" and res.data["section"] == "quarterly"
    assert len(res.data["periods"]) == 13


def test_snapshot_returns_only_the_top_ratios_section_and_shareholding_the_inner_json() -> None:
    srv = Tapetide()
    ad = adapter(srv)
    snap = ad.fetch(resource="financials", symbol="RELIANCE", section="snapshot")
    assert "section" not in srv.bodies[0]["params"]["arguments"]
    assert snap.data["data"]["Face Value"] == 10
    sh = ad.fetch(resource="shareholding", symbol="RELIANCE")
    assert srv.bodies[1]["params"] == {
        "name": "get_shareholding",
        "arguments": {"symbol": "RELIANCE", "type": "quarterly"},
    }
    assert sh.data["data"][0]["data"]["Promoters"]["Jun 2026"] == 50.48
    assert ad.calls == 2


def test_token_never_in_params_cache_or_exception(tmp_path: Path) -> None:
    srv = Tapetide()
    init_stores(tmp_path)
    duck = open_duck(tmp_path / "nivesh.duckdb")
    ad = adapter(srv)
    res = cached_fetch(ad, QUARTERLY, "fundamentals", conn=duck, ttls=Ttls(), redact=False)
    dump = json.dumps(duck.execute("select * from cache_entry").fetchall(), default=str)
    assert token() not in dump and token() not in repr(res) and token() not in repr(ad.__dict__)
    srv.fail_all = httpx.Response(401, json=srv.misc["err_bad_token_401"])
    with pytest.raises(SourceUnavailable) as e:
        adapter(srv).fetch(**QUARTERLY)
    assert token() not in str(e.value) and token() not in repr(e.value)
    duck.close()


def test_missing_token_is_unavailable_with_setup_hint(
    monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    monkeypatch.delenv("TAPETIDE_TOKEN")
    srv = Tapetide()
    ad = adapter(srv)
    with pytest.raises(SourceUnavailable, match="TAPETIDE_TOKEN") as e:
        ad.fetch(**QUARTERLY)
    assert "nivesh secrets set TAPETIDE_TOKEN" in str(e.value) and "tapetide.com" in str(e.value)
    assert srv.calls == 0 and ad.halted
    with pytest.raises(SourceUnavailable):  # halted: still no request
        ad.fetch(**QUARTERLY)
    assert srv.calls == 0


@pytest.mark.parametrize("status", [401, 403])
def test_http_401_and_403_are_unavailable_naming_token_regeneration_and_do_not_retry(
    status: int,
) -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(status, json=srv.misc["err_bad_token_401"])
    ad = adapter(srv)
    with pytest.raises(SourceUnavailable, match=str(status)) as e:
        ad.fetch(**QUARTERLY)
    assert "regenerate" in str(e.value) and "TAPETIDE_TOKEN" in str(e.value)
    assert srv.calls == 1 and ad.halted
    for resource in ({"resource": "shareholding", "symbol": "RELIANCE"}, QUARTERLY):
        with pytest.raises(SourceUnavailable, match="regenerate"):
            ad.fetch(**resource)
    assert srv.calls == 1  # the rejected token is never sent again


QUOTA_TEXT = "Error: Rate limit exceeded: 50 requests per day on the free plan"


def test_iserror_quota_text_with_retry_after_raises_ratelimited_and_halts() -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(
        200, json=envelope("Error: denied", is_error=True), headers={"Retry-After": "3600"}
    )
    ad = adapter(srv, budget=memory_budget(daily=400, monthly=5000))
    with pytest.raises(RateLimited, match="400") as e:
        ad.fetch(**QUARTERLY)
    assert "00:00 IST" in str(e.value) and ad.halted
    with pytest.raises(RateLimited):
        ad.fetch(**QUARTERLY)
    assert srv.calls == 1


def test_iserror_quota_text_without_header_and_http_429_also_raise_ratelimited() -> None:
    for resp in (
        httpx.Response(200, json=envelope(QUOTA_TEXT, is_error=True)),
        httpx.Response(429, text="slow down"),
    ):
        srv = Tapetide()
        srv.fail_all = resp
        with pytest.raises(RateLimited, match="quota reached"):
            adapter(srv, budget=memory_budget(daily=400, monthly=5000)).fetch(**QUARTERLY)


def test_unknown_symbol_is_unavailable_not_empty_and_does_not_halt() -> None:
    srv = Tapetide()
    ad = adapter(srv)
    with pytest.raises(SourceUnavailable, match="no financial statements for ZZNOSUCH"):
        ad.fetch(resource="financials", symbol="ZZNOSUCH", section="quarterly")
    assert not ad.halted and ad.calls == 0  # a failed call is not billed
    assert ad.fetch(**QUARTERLY).data["section"] == "quarterly"


def test_unknown_section_iserror_is_unavailable_with_the_vendor_text_truncated() -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(200, json=srv.misc["err_bad_section"])
    with pytest.raises(SourceUnavailable, match="unknown section") as e:
        adapter(srv).fetch(**QUARTERLY)
    assert len(str(e.value)) < 220


def test_iserror_text_echoing_the_token_is_scrubbed_even_across_the_truncation_limit() -> None:
    for pad in (0, 132):  # 132 puts the token astride the 160-character cut
        srv = Tapetide()
        text = "Error: " + "x" * pad + f" bad credential {token()} supplied " + "y" * 40
        srv.fail_all = httpx.Response(200, json=envelope(text, is_error=True))
        with pytest.raises(SourceUnavailable, match="Tapetide error") as e:
            adapter(srv).fetch(**QUARTERLY)
        msg = str(e.value)
        assert token() not in msg and token()[:5] not in msg and token()[-6:] not in msg


def test_sse_framed_response_is_parsed() -> None:
    srv = Tapetide()
    payload = json.dumps(srv.files["RELIANCE"]["quarterly"])
    sse = f"event: message\ndata: {{}}\n\nevent: message\ndata: {payload}\n\n"
    srv.fail_all = httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    assert adapter(srv).fetch(**QUARTERLY).data["section"] == "quarterly"


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1}),
        httpx.Response(200, json=envelope("not json at all")),
        httpx.Response(200, json=envelope({"data": []})),
        httpx.Response(200, json=envelope({"data": [{"section": "quarterly"}, {"section": "x"}]})),
        httpx.Response(
            200, json=envelope({"data": [{"section": "profit_loss", "periods": [], "data": {}}]})
        ),
        httpx.Response(200, json=envelope({"data": ["row"]})),
    ],
)
def test_malformed_envelope_degrades_to_unavailable_not_a_crash(resp: httpx.Response) -> None:
    srv = Tapetide()
    srv.fail_all = resp
    with pytest.raises(SourceUnavailable, match="unexpected response shape"):
        adapter(srv).fetch(**QUARTERLY)


def test_non_numeric_cell_is_a_data_quality_error() -> None:
    srv = Tapetide()
    sec = inner(srv.files["RELIANCE"]["quarterly"])
    sec["data"][0]["data"]["Sales"]["Jun 2026"] = "1,23,456"
    srv.fail_all = httpx.Response(200, json=envelope(sec))
    with pytest.raises(DataQualityError):
        adapter(srv).fetch(**QUARTERLY)


def test_server_error_maps_to_unavailable_and_does_not_halt() -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(503)
    ad = adapter(srv)
    with pytest.raises(SourceUnavailable, match="HTTP 503"):
        ad.fetch(**QUARTERLY)
    assert not ad.halted


def test_calls_are_paced() -> None:
    srv, sleeps = Tapetide(), []
    ad = adapter(srv, sleeps)
    for section in ("quarterly", "profit_loss", "balance_sheet"):
        ad.fetch(resource="financials", symbol="RELIANCE", section=section)
    assert len(sleeps) == 2 and all(abs(s - 1.1) < 1e-9 for s in sleeps)


def test_default_pacing_is_below_the_vendor_burst_limit() -> None:
    ad = TapetideFundamentals(client(Tapetide()))
    ad.use_budget(memory_budget())
    ad.fetch(**QUARTERLY)  # first call is not delayed
    assert ad._limiter is not None and ad._limiter.interval >= 1.1 - 1e-9


# mapper ---------------------------------------------------------------------------------------

TODAY = date(2026, 10, 13)
CR = Decimal(10) ** 7  # one crore


def sections(company: str) -> dict[str, Any]:
    f = Tapetide().files[company]
    return {k: inner(v)["data"][0] for k, v in f.items()}


def face(company: str) -> Decimal | None:
    misc = Tapetide().misc
    key = "snapshot_reliance" if company == "RELIANCE" else f"synthetic_snapshot_{company.lower()}"
    return face_value_of(inner(misc[key])["data"][0])


def mapped(company: str, fv: Decimal | None = ..., today: date = TODAY) -> Statements:  # type: ignore[assignment]
    return map_statements(sections(company), face(company) if fv is ... else fv, today)


def value(rows: list[Any], item: str, end: date, kind: str = "A") -> Decimal:
    got = [
        r.value for r in rows if r.item == item and r.period_end == end and r.period_type == kind
    ]
    assert len(got) == 1, (item, end, got)
    return got[0]  # type: ignore[no-any-return]


FY26 = date(2026, 3, 31)


def test_reliance_annual_rows() -> None:
    rows = mapped("RELIANCE").reported
    assert value(rows, "revenue", FY26) == Decimal(1055780) * CR
    assert value(rows, "net_income", FY26) == Decimal(95754) * CR
    assert value(rows, "eps", FY26) == Decimal("59.69")
    assert value(rows, "total_equity", FY26) == Decimal(13532 + 890498) * CR
    assert value(rows, "total_debt", FY26) == Decimal(402962) * CR
    assert value(rows, "total_assets", FY26) == Decimal(2177546) * CR
    assert value(rows, "cfo", FY26) == Decimal(192113) * CR
    assert value(rows, "capex", FY26) == Decimal(192113 - 70023) * CR
    assert value(rows, "interest_expense", FY26) == Decimal(27061) * CR
    assert value(rows, "depreciation_amortization", FY26) == Decimal(57688) * CR
    assert {r.currency for r in rows if r.item != "shares_out"} == {"INR"}
    assert {r.filed_at for r in rows if r.period_end == FY26} == {date(2026, 4, 27)}
    items = {r.item for r in rows}
    assert not items & {"income_tax", "pretax_income", "operating_income", "cash", "deposits"}


def test_reliance_depth() -> None:
    m = mapped("RELIANCE")
    rows = m.reported + m.estimated
    assert len({r.period_end for r in rows if r.period_type == "A"}) >= 5
    assert len({r.period_end for r in rows if r.period_type == "Q"}) == 13


def test_ttm_and_null_cells_skipped() -> None:
    secs = sections("RELIANCE")
    secs["profit_loss"]["data"]["Sales"]["Mar 2026"] = None
    m = map_statements(secs, face("RELIANCE"), TODAY)
    rows = m.reported + m.estimated
    assert not [r for r in rows if r.item == "revenue" and r.period_end == FY26
                and r.period_type == "A"]  # fmt: skip
    assert all(r.period_end.month in (3, 6, 9, 12) for r in rows)
    assert not any("TTM" in s for s in m.skipped)  # TTM is a pseudo-period, not a defect


def test_estimated_basis_rows_split_to_the_estimated_group() -> None:
    m = mapped("RELIANCE")
    fy24 = date(2024, 3, 31)
    annual = [r for r in m.estimated if r.period_end == fy24 and r.period_type == "A"]
    assert annual and {r.filed_at for r in annual} == {date(2024, 5, 31)}
    assert not [r for r in m.reported if r.period_end == fy24 and r.period_type == "A"]
    assert [r for r in m.reported if r.period_end == FY26] and not [
        r for r in m.estimated if r.period_end == FY26
    ]
    # quarterly: Sep 2024 is the first declared date, earlier quarters are estimates
    q = date(2024, 9, 30)
    assert {r.filed_at for r in m.reported if r.period_end == q} == {date(2024, 10, 15)}
    assert {r.period_end for r in m.estimated if r.period_type == "Q"} == {
        date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31),
        date(2024, 6, 30),
    }  # fmt: skip


def test_estimated_filing_dates_are_clamped_to_the_fetch_date() -> None:
    m = mapped("RELIANCE", today=date(2024, 5, 1))  # FY24 'estimated' 2024-05-31 is in the future
    assert max(r.filed_at for r in m.reported + m.estimated) <= date(2024, 5, 1)
    assert m.clamped > 0
    assert mapped("RELIANCE").clamped == 0


def test_period_label_to_month_end() -> None:
    from nivesh_adapters.fundamentals_tapetide import period_end

    assert period_end("Feb 2024") == date(2024, 2, 29)
    assert period_end("Mar 2026") == date(2026, 3, 31)
    assert period_end("TTM") is None and period_end("Foo 2024") is None


def test_hdfcbank_maps_revenue_borrowing_and_deposits() -> None:
    rows = mapped("HDFCBANK").reported
    assert value(rows, "revenue", FY26) == Decimal(348615) * CR
    assert value(rows, "total_debt", FY26) == Decimal(588485) * CR
    assert value(rows, "deposits", FY26) == Decimal(3099638) * CR
    assert value(rows, "financing_profit", FY26) == Decimal(-40511) * CR
    items = {r.item for r in rows}
    assert not items & {
        "operating_income",
        "gnpa_pct",
        "nnpa_pct",
        "nim_pct",
        "casa_pct",
        "car_pct",
    }
    # a bank's quarterly depreciation of 0 is "not reported", never stored as zero
    q = [r for r in mapped("HDFCBANK").reported if r.item == "depreciation_amortization"
         and r.period_type == "Q"]  # fmt: skip
    assert q == []


def test_tcs_infy_itc_revenue_matches_recorded_published_values() -> None:
    # ITC: Tapetide Sales is net of excise (78,868 vs 89,913 revenue from operations): disclosed
    for company, crore in (("TCS", 267021), ("INFY", 178650), ("ITC", 78868)):
        assert value(mapped(company).reported, "revenue", FY26) == Decimal(crore) * CR


def test_shares_out_from_equity_capital_and_face_value() -> None:
    rows = mapped("RELIANCE").reported
    assert value(rows, "shares_out", FY26) == Decimal("13532000000")  # 1,353.2 crore shares
    assert [r for r in rows if r.item == "shares_out"] == [
        r for r in rows if r.item == "shares_out" and r.period_end == FY26
    ]  # latest annual period only


def test_missing_face_value_skips_shares_out_with_reason() -> None:
    m = mapped("RELIANCE", None)
    assert not [r for r in m.reported + m.estimated if r.item == "shares_out"]
    assert any("shares_out" in s and "Face Value" in s for s in m.skipped)


def test_period_without_availability_is_skipped_and_listed() -> None:
    secs = sections("RELIANCE")
    secs["profit_loss"]["availability"] = [
        a for a in secs["profit_loss"]["availability"] if a["period"] != "Mar 2025"
    ]
    m = map_statements(secs, face("RELIANCE"), TODAY)
    fy25 = date(2025, 3, 31)
    assert not [r for r in m.reported + m.estimated if r.period_end == fy25
                and r.period_type == "A" and r.item in ("revenue", "net_income")]  # fmt: skip
    assert any("Mar 2025" in s and "filing date" in s for s in m.skipped)


def test_negative_derived_capex_is_skipped_and_listed() -> None:
    secs = sections("RELIANCE")
    secs["cash_flow"]["data"]["Free Cash Flow"]["Mar 2026"] = 200000  # above CFO 192113
    m = map_statements(secs, face("RELIANCE"), TODAY)
    assert not [r for r in m.reported if r.item == "capex" and r.period_end == FY26]
    assert any("capex" in s and "Mar 2026" in s for s in m.skipped)


def test_equity_needs_both_capital_and_reserves() -> None:
    secs = sections("RELIANCE")
    secs["balance_sheet"]["data"]["Reserves"]["Mar 2026"] = None
    rows = map_statements(secs, face("RELIANCE"), TODAY).reported
    assert not [r for r in rows if r.item == "total_equity" and r.period_end == FY26]


def test_shareholding_mapping() -> None:
    payload = inner(Tapetide().misc["shareholding"])
    got = map_shareholding(payload, TODAY)
    by_end = {r.period_end: r for r in got.rows}
    last = by_end[date(2026, 6, 30)]
    assert last.promoter_pct == Decimal("50.48") and last.public_pct == Decimal("8.58")
    assert last.filed_at == date(2026, 7, 21)  # quarter end + 21 days
    assert last.promoter_pledged_pct == Decimal(0)  # status "none" for the latest quarter
    assert all(r.promoter_pledged_pct is None for e, r in by_end.items() if e != last.period_end)
    assert len(got.rows) == 8 and got.clamped == 0


def test_shareholding_pledge_unknown_is_none_and_filing_date_clamped() -> None:
    payload = inner(Tapetide().misc["shareholding"])
    payload["promoter_pledge"] = {"as_of_date": "2026-06-30", "pct_of_promoter_holding": None,
                                  "status": "unknown"}  # fmt: skip
    got = map_shareholding(payload, date(2026, 7, 10))  # before Jun 2026 + 21 days
    last = [r for r in got.rows if r.period_end == date(2026, 6, 30)][0]
    assert last.promoter_pledged_pct is None
    assert last.filed_at == date(2026, 7, 10) and got.clamped == 1
    payload["promoter_pledge"] = {"as_of_date": "2026-06-30", "pct_of_promoter_holding": 12.5,
                                  "status": "pledged"}  # fmt: skip
    got = map_shareholding(payload, TODAY)
    assert [r for r in got.rows if r.promoter_pledged_pct == Decimal("12.5")][0].period_end == date(
        2026, 6, 30
    )


def test_face_value_of_reads_snapshot_or_none() -> None:
    assert face("RELIANCE") == Decimal(10) and face("INFY") == Decimal(5)
    assert face_value_of(None) is None and face_value_of({"data": {}}) is None
    assert face_value_of({"data": {"Face Value": 0}}) is None


# review round 2 -------------------------------------------------------------------------------

SHARE = {"resource": "shareholding", "symbol": "RELIANCE"}
SNAP = {"resource": "financials", "symbol": "RELIANCE", "section": "snapshot"}
DEEP = "[" * 100_000


def test_shareholder_count_row_in_the_millions_is_not_capped_as_a_percentage() -> None:
    got = adapter(Tapetide()).fetch(**SHARE)
    counts = got.data["data"][0]["data"]["No. of Shareholders"]
    assert min(counts.values()) > 1_000_000  # the recorded count row, not a percentage
    rows = map_shareholding(got.data, TODAY).rows
    assert len(rows) == 8 and rows[-1].promoter_pct == Decimal("50.48")


def test_percentage_rows_are_still_bounded_and_count_rows_must_be_scalars() -> None:
    for row, cell in (("Promoters", 1e9), ("Public", [1]), ("No. of Shareholders", [[1]])):
        srv = Tapetide()
        payload = inner(srv.misc["shareholding"])
        payload["data"][0]["data"][row]["Jun 2026"] = cell
        srv.misc["shareholding"] = envelope(payload)
        with pytest.raises(DataQualityError):
            adapter(srv).fetch(**SHARE)


def _whole_body(srv: Tapetide) -> None:
    srv.fail_all = httpx.Response(200, text='{"a":' + DEEP)


def _inner_text(srv: Tapetide) -> None:
    srv.fail_all = httpx.Response(200, json=envelope(DEEP))


def _snapshot_text(srv: Tapetide) -> None:
    srv.misc["snapshot_reliance"] = envelope('{"data":' + DEEP)


def _shareholding_text(srv: Tapetide) -> None:
    srv.misc["shareholding"] = envelope('{"data":' + DEEP)


@pytest.mark.parametrize(
    ("setup", "params"),
    [
        (_whole_body, QUARTERLY),
        (_inner_text, QUARTERLY),
        (_snapshot_text, SNAP),
        (_shareholding_text, SHARE),
    ],  # fmt: skip
)
def test_deeply_nested_json_is_a_shape_error_not_a_recursion_crash(
    setup: Any, params: dict[str, str]
) -> None:
    srv = Tapetide()
    setup(srv)
    with pytest.raises(SourceUnavailable, match="unexpected response shape"):
        adapter(srv).fetch(**params)


def test_periods_limit_error_is_not_mistaken_for_the_daily_quota() -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(200, json=envelope("periods limit exceeded (max 40)",
                                                     is_error=True))  # fmt: skip
    ad = adapter(srv)
    with pytest.raises(SourceUnavailable, match="Tapetide error") as e:
        ad.fetch(**QUARTERLY)
    assert not isinstance(e.value, RateLimited) and not ad.halted


def test_transport_failure_text_never_reaches_the_message_even_with_the_token_in_it() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach host with {token()} and {token()[:6]}")

    ad = TapetideFundamentals(httpx.Client(transport=httpx.MockTransport(boom)),
                              limiter=adapter(Tapetide())._limiter)  # fmt: skip
    ad.use_budget(memory_budget())
    with pytest.raises(SourceUnavailable) as e:
        ad.fetch(**QUARTERLY)
    assert token() not in str(e.value) and token()[:6] not in str(e.value)
    assert "ConnectError" in str(e.value) and e.value.__cause__ is None


def test_token_with_trailing_newline_is_stripped_before_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAPETIDE_TOKEN", token() + "\r\n")
    srv = Tapetide()
    assert adapter(srv).fetch(**QUARTERLY).data["section"] == "quarterly"
    assert srv.requests[0].headers["authorization"] == f"{scheme()} {token()}"


# mapper additions -----------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [{"x": 1}, [10], "10", float("nan"), float("inf"), True])
def test_face_value_of_returns_none_for_anything_that_is_not_a_number(bad: Any) -> None:
    assert face_value_of({"data": {"Face Value": bad}}) is None


def test_future_periods_are_skipped_and_listed_not_given_a_filing_date_before_their_end() -> None:
    secs = sections("RELIANCE")
    p = secs["profit_loss"]
    p["periods"].append("Mar 2030")
    p["data"]["Sales"]["Mar 2030"] = 5
    p["availability"].append({"period": "Mar 2030", "available_from": "2030-05-01",
                              "basis": "reported"})  # fmt: skip
    m = map_statements(secs, face("RELIANCE"), TODAY)
    assert not [r for r in m.reported + m.estimated if r.period_end.year == 2030]
    assert any("Mar 2030" in s and "after today" in s for s in m.skipped)
    assert all(r.filed_at >= r.period_end for r in m.reported + m.estimated)


def test_shareholding_future_and_year_9999_periods_skip_only_that_period() -> None:
    payload = inner(Tapetide().misc["shareholding"])
    table = payload["data"][0]["data"]
    for label in ("Dec 9999", "Mar 2030"):
        table["Promoters"][label] = 50.0
        table["Public"][label] = 9.0
    got = map_shareholding(payload, TODAY)
    assert len(got.rows) == 8 and max(r.period_end for r in got.rows) == date(2026, 6, 30)
    assert any("Dec 9999" in s and "after today" in s for s in got.skipped)


def test_shareholding_without_promoter_or_public_rows_gives_a_skipped_reason() -> None:
    payload = inner(Tapetide().misc["shareholding"])
    payload["data"][0]["data"] = {"FIIs": {"Jun 2026": 17.2}}
    got = map_shareholding(payload, TODAY)
    assert got.rows == [] and any("Promoters" in s and "Public" in s for s in got.skipped)


def test_pledge_status_none_alone_is_zero_and_numeric_zero_alone_is_zero() -> None:
    payload = inner(Tapetide().misc["shareholding"])
    for pledge in (
        {"as_of_date": "2026-06-30", "pct_of_promoter_holding": None, "status": "none"},
        {"as_of_date": "2026-06-30", "pct_of_promoter_holding": 0},
    ):
        payload["promoter_pledge"] = pledge
        last = map_shareholding(payload, TODAY).rows[-1]
        assert last.promoter_pledged_pct == Decimal(0)
    payload["promoter_pledge"] = {"as_of_date": "2026-06-30", "pct_of_promoter_holding": None,
                                  "status": "unknown"}  # fmt: skip
    assert map_shareholding(payload, TODAY).rows[-1].promoter_pledged_pct is None


# --- #130 hardening ---


@pytest.mark.parametrize("pct", ["abc", "12.5%", True, [1], float("nan"), float("inf")])
def test_unparsable_pledge_pct_text_bool_list_or_nan_is_none_and_keeps_promoter_and_public_rows(
    pct: Any,
) -> None:
    srv = Tapetide()
    payload = inner(srv.misc["shareholding"])
    payload["promoter_pledge"].update(pct_of_promoter_holding=pct, status="unknown")
    srv.misc["shareholding"] = envelope(payload)
    rows = map_shareholding(adapter(srv).fetch(**SHARE).data, TODAY).rows
    assert len(rows) == 8
    assert rows[-1].promoter_pct == Decimal("50.48") and rows[-1].public_pct == Decimal("8.58")
    assert rows[-1].promoter_pledged_pct is None


@pytest.mark.parametrize("bad", [["Mar 2026"], {"p": "Mar 2026"}])
def test_availability_entry_with_an_unhashable_period_is_skipped_on_its_own(bad: Any) -> None:
    secs = sections("RELIANCE")
    entry = {"period": bad, "available_from": "2026-04-27", "basis": "reported"}
    secs["profit_loss"]["availability"].insert(0, entry)
    got = map_statements(secs, face("RELIANCE"), TODAY)
    assert got.reported == mapped("RELIANCE").reported
    assert got.estimated == mapped("RELIANCE").estimated
    assert value(got.reported, "revenue", FY26) == Decimal(1055780) * CR


@pytest.mark.parametrize(
    "text",
    [
        "Error: Not found (tapetide-api): Company not found: X",
        "Error: Symbol not found: X",
        "Error: Section not found: X",
    ],
)
def test_company_symbol_and_section_not_found_texts_read_as_no_data_for_both_call_kinds(
    text: str,
) -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(200, json=envelope(text, is_error=True))
    with pytest.raises(
        SourceUnavailable, match="Tapetide has no financial statements for RELIANCE"
    ):
        adapter(srv).fetch(**QUARTERLY)
    with pytest.raises(SourceUnavailable, match="Tapetide has no shareholding data for RELIANCE"):
        adapter(srv).fetch(**SHARE)


@pytest.mark.parametrize(
    ("text", "tail"),
    [
        ("Error: Not found (tapetide-api): token not found", "token not found"),
        ("Error: Not found (tapetide-api): nothing here", "nothing here"),
    ],
)
def test_token_not_found_and_bare_not_found_prefix_are_failures_not_an_empty_result(
    text: str, tail: str
) -> None:
    for params in (QUARTERLY, SHARE):
        srv = Tapetide()
        srv.fail_all = httpx.Response(200, json=envelope(text, is_error=True))
        ad = adapter(srv)
        with pytest.raises(SourceUnavailable, match="Tapetide error") as e:
            ad.fetch(**params)
        assert tail in str(e.value) and "has no" not in str(e.value)
        assert not ad.halted


@pytest.mark.parametrize("cell", [True, False])
def test_boolean_cell_is_a_data_quality_error_not_a_number(cell: bool) -> None:
    srv = Tapetide()
    sec = inner(srv.files["RELIANCE"]["quarterly"])
    sec["data"][0]["data"]["Sales"]["Jun 2026"] = cell
    srv.fail_all = httpx.Response(200, json=envelope(sec))
    with pytest.raises(DataQualityError, match="finite number"):
        adapter(srv).fetch(**QUARTERLY)


# --- #131 hardening ---


@pytest.mark.parametrize(
    "v",
    [0, 1, 1.5, -3, 0.0, pytest.param(10**400, id="huge"), pytest.param(-(10**400), id="-huge")],
)
def test_real_is_a_finite_number_that_is_not_a_bool(v: Any) -> None:
    assert _real(v) is True


@pytest.mark.parametrize(
    "v",
    [True, False, None, "1", [1], {"a": 1}, float("nan"), float("inf"), float("-inf"), Decimal(1)],
)
def test_real_rejects_bools_text_containers_nan_inf_and_decimals(v: Any) -> None:
    assert _real(v) is False


def test_dec_of_a_huge_int_is_a_decimal_not_an_overflow() -> None:
    assert _dec(10**400) == Decimal(10) ** 400  # math.isfinite(10**400) raises OverflowError
    assert _dec(1.5) == Decimal("1.5") and _dec(True) is None and _dec(float("nan")) is None


def _pledge_payload(pct: Any, status: str) -> dict[str, Any]:
    payload = inner(Tapetide().misc["shareholding"])
    payload["promoter_pledge"].update(pct_of_promoter_holding=pct, status=status)
    return payload


def test_unparsable_pledge_text_with_an_unknown_status_adds_exactly_one_skipped_line() -> None:
    got = map_shareholding(_pledge_payload("abc", "unknown"), TODAY)
    lines = [s for s in got.skipped if "promoter pledge" in s]
    assert len(lines) == 1 and "not a number" in lines[0]
    assert len(got.rows) == 8 and got.rows[-1].promoter_pledged_pct is None
    assert got.rows[-1].promoter_pct == Decimal("50.48") and got.rows[-1].public_pct == Decimal(
        "8.58"
    )


def test_status_none_keeps_pledge_zero_and_adds_no_line_and_a_null_pct_adds_none() -> None:
    none = map_shareholding(_pledge_payload("abc", "none"), TODAY)
    assert none.rows[-1].promoter_pledged_pct == Decimal(0)
    assert not [s for s in none.skipped if "promoter pledge" in s]
    null = map_shareholding(_pledge_payload(None, "unknown"), TODAY)
    assert null.rows[-1].promoter_pledged_pct is None
    assert not [s for s in null.skipped if "promoter pledge" in s]
    good = map_shareholding(_pledge_payload(12.5, "pledged"), TODAY)
    assert not [s for s in good.skipped if "promoter pledge" in s]


def test_a_company_not_found_reply_marks_the_symbol_unknown_and_nothing_else_does() -> None:
    def ask(text: str, **params: str) -> TapetideFundamentals:
        srv = Tapetide()
        srv.fail_all = httpx.Response(200, json=envelope(text, is_error=True))
        ad = adapter(srv)
        with pytest.raises(SourceUnavailable, match="Tapetide has no"):
            ad.fetch(**{"resource": "financials", "symbol": "X", "section": "quarterly", **params})
        return ad

    assert ask("Error: Not found (tapetide-api): Company not found: X").unknown_symbols == {"X"}
    assert ask("Error: Symbol not found: X").unknown_symbols == {"X"}
    assert ask("Error: Section not found: quarterly").unknown_symbols == set()
    shareholding = ask("Error: Company not found: X", resource="shareholding")
    assert shareholding.unknown_symbols == set()  # only a statements reply short-circuits


# --- #131 quota ---

NOON = datetime(2026, 10, 9, 6, 30, tzinfo=UTC)  # 12:00 IST on 2026-10-09, not a month start


def noon() -> datetime:
    return NOON


VENDOR_QUOTA_REPLIES = pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(429, text="slow down"),
        httpx.Response(200, json=envelope(QUOTA_TEXT, is_error=True)),
        httpx.Response(
            200, json=envelope("Error: denied", is_error=True), headers={"Retry-After": "1"}
        ),
    ],
    ids=["http-429", "quota-text", "retry-after"],
)


def counted(c: sqlite3.Connection) -> list[str | None]:
    """Outcome of every real (non-manual) ledger row, in order; None while it is unsettled."""
    return [r[0] for r in c.execute("SELECT outcome FROM vendor_call WHERE kind = 'call' "
                                    "ORDER BY id")]  # fmt: skip


def test_a_budgetless_adapter_refuses_to_post_and_does_not_read_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAPETIDE_TOKEN")  # were it read first, the message would name the token
    srv = Tapetide()
    ad = TapetideFundamentals(client(srv))
    with pytest.raises(NotConfigured, match="quota budget") as e:
        ad.fetch(**QUARTERLY)
    assert "TAPETIDE_TOKEN" not in str(e.value) and ad.halted and srv.calls == 0
    with pytest.raises(NotConfigured):  # halted: still nothing sent
        ad.fetch(**QUARTERLY)
    with pytest.raises(NotConfigured, match="quota budget"):  # the chokepoint itself, not `_call`
        TapetideFundamentals(client(srv))._rpc("dummy", "get_financials", {"symbol": "X"})
    assert srv.calls == 0


def test_every_post_reserves_first_and_the_ledger_matches_the_requests(tmp_path: Path) -> None:
    srv, c = Tapetide(), ledger(tmp_path / "l.sqlite")
    other = sqlite3.connect(tmp_path / "l.sqlite")  # a second process looking at the ledger
    seen: list[tuple[int, Any]] = []

    def spy(req: httpx.Request) -> httpx.Response:
        n, last = other.execute(
            "SELECT COUNT(*), (SELECT outcome FROM vendor_call ORDER BY id DESC LIMIT 1) "
            "FROM vendor_call"
        ).fetchone()
        seen.append((n, last))  # the row is committed, and not yet settled, when the POST leaves
        return srv(req)

    ad = adapter(spy, budget=memory_budget(c, daily=400, monthly=5000))
    replies: list[httpx.Response | None] = [
        None,
        httpx.Response(200, json=envelope("Error: nope", is_error=True)),
        httpx.Response(500),
        httpx.Response(200, text="<html>bad gateway</html>"),
        httpx.Response(429, text="slow down"),
    ]
    for reply in replies:
        srv.fail_all = reply
        with suppress(MarketDataError):
            ad.fetch(**QUARTERLY)
    assert srv.calls == 5 and seen == [(i, None) for i in range(1, 6)]
    assert counted(c) == ["ok", "error", "error", "error", "halted"]
    assert ad.calls == 1  # only the first reply was billed


def test_a_refused_reserve_sends_nothing_and_halts_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    srv, c = Tapetide(), ledger()
    b = memory_budget(c, daily=1, monthly=None)
    ad = adapter(srv, budget=b)
    ad.fetch(**QUARTERLY)  # the one call the plan allows
    with pytest.raises(QuotaExceeded, match="daily quota reached: 1 of 1 used") as e:
        ad.fetch(resource="shareholding", symbol="RELIANCE")
    assert isinstance(e.value, RateLimited) and ad.halted and srv.calls == 1
    monkeypatch.setattr(b, "reserve", lambda *a, **k: pytest.fail("a halted run must not reserve"))
    for _ in range(2):
        with pytest.raises(QuotaExceeded):
            ad.fetch(**QUARTERLY)
    assert srv.calls == 1 and counted(c) == ["ok"]


def test_failed_and_garbage_replies_still_count_and_are_never_refunded() -> None:
    srv, c = Tapetide(), ledger()
    b = memory_budget(c)
    ad = adapter(srv, budget=b)
    for reply in (httpx.Response(500), httpx.Response(200, text="<html>x</html>"),
                  httpx.Response(200, json=envelope("Error: nope", is_error=True))):  # fmt: skip
        srv.fail_all = reply
        with pytest.raises(SourceUnavailable):
            ad.fetch(**QUARTERLY)
    assert counted(c) == ["error"] * 3 and b.status().day.used == 3 and ad.calls == 0
    srv.fail_all = httpx.Response(401, json=srv.misc["err_bad_token_401"])
    with pytest.raises(SourceUnavailable, match="rejected"):
        ad.fetch(**QUARTERLY)
    assert counted(c)[-1] == "halted" and b.status().day.used == 4 and b.attempts == 4


def test_no_token_and_halted_paths_never_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    srv, c = Tapetide(), ledger()
    monkeypatch.delenv("TAPETIDE_TOKEN")
    ad = adapter(srv, budget=memory_budget(c))
    with pytest.raises(NotConfigured, match="TAPETIDE_TOKEN"):
        ad.fetch(**QUARTERLY)
    assert counted(c) == [] and srv.calls == 0
    monkeypatch.setenv("TAPETIDE_TOKEN", token())
    srv.fail_all = httpx.Response(401, json=srv.misc["err_bad_token_401"])
    ad = adapter(srv, budget=memory_budget(c))
    for _ in range(3):
        with pytest.raises(SourceUnavailable):
            ad.fetch(**QUARTERLY)
    assert counted(c) == ["halted"] and srv.calls == 1  # one reservation, then silence


@VENDOR_QUOTA_REPLIES
def test_vendor_quota_text_halts_and_the_message_carries_the_configured_plan(
    reply: httpx.Response,
) -> None:
    srv = Tapetide()
    srv.fail_all = reply
    ad = adapter(srv, budget=memory_budget(daily=300, monthly=2000))
    with pytest.raises(RateLimited) as e:
        ad.fetch(**QUARTERLY)
    msg = str(e.value)
    assert "daily 300" in msg and "monthly 2,000" in msg and "00:00 IST" in msg and ad.halted
    assert "free plan" not in msg and "50 calls/day" not in msg


@pytest.mark.parametrize(
    "text",
    [
        "Error: monthly limit reached for this plan",
        "Error: monthly cap hit",
        "Error: 5000 calls allowed per month",
    ],
)
def test_monthly_wording_also_halts_and_fills_the_month_not_the_day(text: str) -> None:
    srv, c = Tapetide(), ledger()
    srv.fail_all = httpx.Response(200, json=envelope(text, is_error=True))
    b = memory_budget(c, daily=400, monthly=5000, now=noon)
    ad = adapter(srv, budget=b)
    with pytest.raises(RateLimited, match="quota reached") as e:
        ad.fetch(**QUARTERLY)
    assert not isinstance(e.value, QuotaExceeded) and ad.halted
    st = b.status()
    assert st.month.used == 5000 and st.day.used == 1  # the month is full; today is not


@pytest.mark.parametrize(
    "text",
    [
        "Error: daily limit reached: 400 requests per day (5000 per month plan)",
        "Error: quota per day exceeded; your monthly cap is 5000",
        "Error: rate limit, 400/day of the 5000 per month allowance used",
    ],
)
def test_daily_wording_wins_over_a_mention_of_the_monthly_plan(text: str) -> None:
    """A reply that names both windows fills the day only: burning the month for a daily limit
    cannot be undone (the ledger is append-only)."""
    srv, c = Tapetide(), ledger()
    srv.fail_all = httpx.Response(200, json=envelope(text, is_error=True))
    b = memory_budget(c, daily=400, monthly=5000, now=noon)
    ad = adapter(srv, budget=b)
    with pytest.raises(RateLimited, match="quota reached"):
        ad.fetch(**QUARTERLY)
    st = b.status()
    assert st.day.used == 400 and st.month.used == 400  # today is full; the month only has today


def test_a_benign_monthly_word_is_not_a_quota_halt() -> None:
    srv = Tapetide()
    srv.fail_all = httpx.Response(200, json=envelope("Error: monthly report unavailable",
                                                     is_error=True))  # fmt: skip
    ad = adapter(srv)
    with pytest.raises(SourceUnavailable, match="Tapetide error") as e:
        ad.fetch(**QUARTERLY)
    assert not isinstance(e.value, RateLimited) and not ad.halted


@VENDOR_QUOTA_REPLIES
def test_a_vendor_quota_halt_is_persisted_so_a_new_process_spends_nothing(
    reply: httpx.Response,
) -> None:
    c, srv = ledger(), Tapetide()
    srv.fail_all = reply
    first = memory_budget(c, daily=400, monthly=5000, now=noon)
    with pytest.raises(RateLimited):
        adapter(srv, budget=first).fetch(**QUARTERLY)
    assert srv.calls == 1 and first.status().day.used == 400  # today topped up to the cap
    srv.fail_all = None
    second = memory_budget(c, daily=400, monthly=5000, now=noon)  # a new process, the same ledger
    with pytest.raises(QuotaExceeded, match="daily quota reached: 400 of 400 used"):
        adapter(srv, budget=second).fetch(**QUARTERLY)
    assert srv.calls == 1 and second.status().day.used == 400  # nothing spent, nothing added


def test_a_halt_when_the_window_is_already_full_adds_no_manual_row() -> None:
    c, srv = ledger(), Tapetide()
    b = memory_budget(c, daily=2, monthly=None, now=noon, priority="holdings")
    adapter(srv, budget=b).fetch(**QUARTERLY)
    adapter(srv, budget=b).fetch(**QUARTERLY)  # now 2 of 2
    assert b.status().day.used == 2
    again = memory_budget(c, daily=5, monthly=None, now=noon, priority="holdings")
    srv.fail_all = httpx.Response(
        429, text="slow down"
    )  # the vendor stops us at 2 of a belief of 5
    with pytest.raises(RateLimited):
        adapter(srv, budget=again).fetch(**QUARTERLY)
    assert again.status().day.used == 5 and c.execute(
        "SELECT COUNT(*) FROM vendor_call WHERE kind = 'manual'").fetchone() == (1,)  # fmt: skip


class _LockedLedger:
    """A ledger whose database is locked: every statement fails the way SQLite reports it."""

    def execute(self, *args: Any, **kw: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")


def test_an_unavailable_ledger_halts_with_a_reason_and_zero_requests() -> None:
    srv = Tapetide()
    ad = adapter(srv, budget=Budget(_LockedLedger(), "tapetide", memory_budget().plan))  # type: ignore[arg-type]
    with pytest.raises(SourceUnavailable, match="quota ledger unavailable") as e:
        ad.fetch(**QUARTERLY)
    assert not isinstance(e.value, RateLimited) and ad.halted and srv.calls == 0
    with pytest.raises(SourceUnavailable, match="quota ledger unavailable"):
        ad.fetch(**QUARTERLY)
    assert srv.calls == 0


def test_a_ledger_that_fails_after_the_reply_does_not_lose_the_reply() -> None:
    srv, c = Tapetide(), ledger()

    class NoSettle(Budget):
        def settle(self, call_id: int, outcome: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

    plan = memory_budget().plan
    res = adapter(srv, budget=NoSettle(c, "tapetide", plan)).fetch(**QUARTERLY)
    assert res.data["section"] == "quarterly" and counted(c) == [None]  # counted, never lost


def test_a_ledger_that_fails_while_recording_a_vendor_halt_still_halts() -> None:
    srv, c = Tapetide(), ledger()
    srv.fail_all = httpx.Response(429, text="slow down")

    class NoExhaust(Budget):
        def exhaust(self, window: Any) -> int:
            raise sqlite3.OperationalError("database is locked")

    ad = adapter(srv, budget=NoExhaust(c, "tapetide", memory_budget().plan))
    with pytest.raises(RateLimited, match="quota reached") as e:
        ad.fetch(**QUARTERLY)
    assert "the configured plan" in str(e.value) and ad.halted and srv.calls == 1
