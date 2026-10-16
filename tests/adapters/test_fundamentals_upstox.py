"""Upstox fundamentals skeleton (ADR-0016): disabled by default, and every wire format here is
UNVERIFIED (hand-written from the public API documentation; no live Upstox call has ever been
made). Fixtures and MockTransport only, no network."""

import dataclasses
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from nivesh_adapters import fundamentals_upstox
from nivesh_adapters.edgar import RateLimiter
from nivesh_adapters.fundamentals_upstox import (
    ANNUAL_LAG,
    QUARTERLY_LAG,
    SHAREHOLDING_LAG,
    UpstoxFundamentals,
    UpstoxProvider,
    map_corporate_actions,
    map_shareholding,
    map_statements,
)
from nivesh_adapters.market_ingest import Run
from nivesh_adapters.providers import Capability, CollectContext, provider_class
from nivesh_adapters.quality import DataQualityError
from nivesh_core.errors import NotConfigured, QuotaExceeded, RateLimited, SourceUnavailable
from nivesh_core.fundamentals_config import PlanSettings, ProviderSpec
from nivesh_core.quota import Budget, set_limit
from nivesh_mcp.base import write_methods
from tests.adapters import test_provider_contract as contract
from tests.adapters.test_provider_contract import World
from tests.conftest import MemoryKeyring
from tests.provider_fx import (
    HARNESSES,
    TODAY,
    UPSTOX_CRED,
    UPSTOX_ISIN,
    UpstoxVendor,
    upstox_adapter,
    upstox_cred,
    upstox_doc,
)
from tests.tapetide_fx import ledger, scheme

world = contract.world  # the shared fixture: a migrated store with RELIANCE (and its ISIN)
YEARLY = {"resource": "income_statement", "isin": UPSTOX_ISIN, "time_period": "yearly"}
BALANCE = {"resource": "balance_sheet", "isin": UPSTOX_ISIN}
FY, FY_PRIOR = date(2026, 3, 31), date(2025, 3, 31)


@pytest.fixture(autouse=True)
def _cred(monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring) -> None:
    """The env var wins in `get_secret`; the fake keychain keeps the real one out of every test."""
    monkeypatch.setenv(UPSTOX_CRED, upstox_cred())


# --- transport: one GET per endpoint, the credential only in the authorization header ---


@pytest.mark.parametrize(
    ("params", "path", "query"),
    [
        (YEARLY, "income-statement", {"type": "consolidated", "time_period": "yearly"}),
        (
            {**YEARLY, "time_period": "quarterly"},
            "income-statement",
            {"type": "consolidated", "time_period": "quarterly"},
        ),
        (BALANCE, "balance-sheet", {"type": "consolidated", "fs": "true"}),
        ({**BALANCE, "resource": "cash_flow"}, "cash-flow", {"type": "consolidated", "fs": "true"}),
        ({**BALANCE, "resource": "share_holdings"}, "share-holdings", {}),
        ({**BALANCE, "resource": "corporate_actions"}, "corporate-actions", {}),
    ],
)
def test_each_endpoint_is_a_get_with_the_documented_path_and_query(
    params: dict[str, Any], path: str, query: dict[str, str]
) -> None:
    vendor = UpstoxVendor()
    res = upstox_adapter(vendor).fetch(**params)
    req = vendor.requests[0]
    assert req.method == "GET" and req.content == b""
    assert str(req.url.copy_with(query=None)) == (
        f"https://api.upstox.com/v2/fundamentals/{UPSTOX_ISIN}/{path}"
    )
    assert dict(req.url.params) == query and req.headers["accept"] == "application/json"
    assert res.source == "upstox" and res.data["status"] == "success"


def test_the_credential_is_sent_only_in_the_authorization_header() -> None:
    vendor, cred = UpstoxVendor(), upstox_cred()
    ad = upstox_adapter(vendor)
    res = ad.fetch(**YEARLY)
    req = vendor.requests[0]
    assert req.headers["authorization"] == f"{scheme()} {cred}"
    others = [str(req.url), req.content.decode(), *(v for k, v in req.headers.items()
              if k != "authorization")]  # fmt: skip
    assert not any(cred in x for x in others)
    assert cred not in repr(ad) and cred not in json.dumps(res.data)


def test_a_token_with_a_pasted_newline_is_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UPSTOX_CRED, f"{upstox_cred()}\n")
    vendor = UpstoxVendor()
    upstox_adapter(vendor).fetch(**YEARLY)
    assert vendor.requests[0].headers["authorization"] == f"{scheme()} {upstox_cred()}"


def test_no_token_raises_not_configured_names_the_secret_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(UPSTOX_CRED)
    vendor = UpstoxVendor()
    ad = upstox_adapter(vendor)
    with pytest.raises(NotConfigured, match=UPSTOX_CRED) as e:
        ad.fetch(**YEARLY)
    assert f"nivesh secrets set {UPSTOX_CRED}" in str(e.value) and vendor.calls == 0
    with pytest.raises(NotConfigured, match=UPSTOX_CRED):  # halted: same answer, no request
        ad.fetch(**BALANCE)
    assert vendor.calls == 0


def test_an_isin_that_is_not_an_isin_never_reaches_the_url() -> None:
    vendor = UpstoxVendor()
    for bad in ("../income-statement", "ine002a01018", "INE002A0101", f"{UPSTOX_ISIN}?x=1"):
        with pytest.raises(ValueError, match="ISIN"):
            upstox_adapter(vendor).fetch(resource="balance_sheet", isin=bad)
    assert vendor.calls == 0


def test_an_unknown_resource_or_time_period_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown resource"):
        upstox_adapter(UpstoxVendor()).fetch(resource="key_ratios", isin=UPSTOX_ISIN)
    with pytest.raises(ValueError, match="time_period"):
        upstox_adapter(UpstoxVendor()).fetch(**{**YEARLY, "time_period": "monthly"})


# --- failures: documented error codes, halts, hostile bodies ---


@pytest.mark.parametrize("status", [400, 200])
@pytest.mark.parametrize(
    ("key", "code"),
    [
        ("err_invalid_isin", "UDAPI1206"),
        ("err_invalid_type", "UDAPI1207"),
        ("err_invalid_time_period", "UDAPI1208"),
    ],
)
def test_documented_error_codes_are_source_unavailable_with_the_code_and_do_not_halt(
    key: str, code: str, status: int
) -> None:
    vendor = UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(status, json=vendor.misc[key])
    ad = upstox_adapter(vendor)
    with pytest.raises(SourceUnavailable, match=code) as e:
        ad.fetch(**YEARLY)
    assert upstox_cred() not in str(e.value)
    assert ad.fetch(**BALANCE).data["status"] == "success" and vendor.calls == 2  # not halted


def test_a_rejected_token_halts_the_adapter_and_names_the_fix() -> None:
    vendor = UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(401, json=vendor.misc["err_bad_token_401"])
    ad = upstox_adapter(vendor)
    with pytest.raises(SourceUnavailable, match=r"rejected the token \(HTTP 401\)") as e:
        ad.fetch(**YEARLY)
    assert f"nivesh secrets set {UPSTOX_CRED}" in str(e.value)
    with pytest.raises(SourceUnavailable, match="rejected the token"):
        ad.fetch(**BALANCE)
    assert vendor.calls == 1  # the second call was refused locally


def test_a_rate_limit_halts_the_adapter_with_no_retry() -> None:
    vendor = UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(429)
    ad = upstox_adapter(vendor)
    with pytest.raises(RateLimited, match="rate-limited"):
        ad.fetch(**YEARLY)
    with pytest.raises(RateLimited):
        ad.fetch(**BALANCE)
    assert vendor.calls == 1


def test_other_http_statuses_and_a_vendor_error_without_a_code_are_source_unavailable() -> None:
    vendor = UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(500, text="<html>bad gateway</html>")
    vendor.fail["balance-sheet"] = httpx.Response(400, json={"status": "error", "errors": "boom"})
    ad = upstox_adapter(vendor)
    with pytest.raises(SourceUnavailable, match="HTTP 500"):
        ad.fetch(**YEARLY)
    with pytest.raises(SourceUnavailable, match="HTTP 400"):
        ad.fetch(**BALANCE)


def test_an_error_text_that_echoes_the_credential_is_scrubbed_and_cut() -> None:
    vendor, cred = UpstoxVendor(), upstox_cred()
    text = f"bad {cred}‮\x1b[31m request " + "x" * 400
    err = {"status": "error", "errors": [{"errorCode": "UDAPI1206", "message": text}]}
    vendor.fail["income-statement"] = httpx.Response(400, json=err)
    with pytest.raises(SourceUnavailable) as e:
        upstox_adapter(vendor).fetch(**YEARLY)
    msg = str(e.value)
    assert cred not in msg and "‮" not in msg and "\x1b" not in msg and len(msg) < 200


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[1, 2]",
        '{"status": "success"}',
        '{"status": "success", "data": [1]}',
        '{"status": "failure", "data": {}}',
        "[" * 100000,  # a bracket bomb overflows the parser's stack
    ],
)
def test_an_unexpected_body_is_an_unexpected_shape_not_a_crash(body: str) -> None:
    vendor = UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(200, text=body)
    with pytest.raises(SourceUnavailable, match="unexpected response shape"):
        upstox_adapter(vendor).fetch(**YEARLY)


def test_a_transport_error_is_reduced_to_its_type_name() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach host with {upstox_cred()}")

    with pytest.raises(SourceUnavailable, match=r"request failed \(ConnectError\)") as e:
        upstox_adapter(boom).fetch(**YEARLY)
    assert upstox_cred() not in str(e.value) and e.value.__cause__ is None


@pytest.mark.parametrize(
    ("cell", "why"),
    [(10**13, "magnitude"), (float("nan"), "finite"), (float("inf"), "finite")],
)
def test_out_of_bounds_numbers_are_bad_data(cell: float, why: str) -> None:
    vendor = UpstoxVendor()
    body = json.dumps(upstox_doc("income_statement")["yearly"]).replace("1350.75", json.dumps(cell))
    vendor.fail["income-statement"] = httpx.Response(200, text=body)  # NaN and Infinity parse
    with pytest.raises(DataQualityError, match=why):
        upstox_adapter(vendor).fetch(**YEARLY)


def test_validation_does_not_recurse_into_a_deep_document() -> None:
    deep: Any = 1
    for _ in range(600):
        deep = [deep]
    vendor = UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(200, json={"status": "success", "data": deep})
    with pytest.raises(SourceUnavailable, match="unexpected response shape"):
        upstox_adapter(vendor).fetch(**YEARLY)  # data must be an object
    vendor.fail["income-statement"] = httpx.Response(
        200, json={"status": "success", "data": {"history": deep}}
    )
    assert upstox_adapter(vendor).fetch(**YEARLY).data["status"] == "success"


# --- pacing and the optional quota ledger ---


def test_pacing_is_off_by_default_and_follows_configure(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[RateLimiter] = []

    def limiter(per_sec: float) -> RateLimiter:  # a fake clock: the test never sleeps
        made.append(RateLimiter(per_sec, lambda: 0.0, lambda s: None))
        return made[-1]

    monkeypatch.setattr(fundamentals_upstox, "RateLimiter", limiter)
    vendor = UpstoxVendor()
    upstox_adapter(vendor).fetch(**YEARLY)
    assert made == []  # no rate limit is documented: no pacing unless a plan asks for it
    paced = upstox_adapter(vendor)
    paced.configure(secret_name=UPSTOX_CRED, min_interval_s=2.5)
    paced.fetch(**YEARLY)
    paced.fetch(**BALANCE)
    assert [m.interval for m in made] == [pytest.approx(2.5)]


def vendor_rows(conn: sqlite3.Connection) -> list[tuple[str, str | None, str | None]]:
    cur = conn.execute("select vendor, tool, outcome from vendor_call order by id")
    return [(str(a), b, c) for a, b, c in cur.fetchall()]


def test_a_plan_budget_gates_every_request_and_halts_when_spent() -> None:
    conn, vendor = ledger(), UpstoxVendor()
    vendor.fail["cash-flow"] = httpx.Response(400, json=vendor.misc["err_invalid_type"])
    ad = upstox_adapter(vendor)
    ad.use_budget(Budget(conn, "upstox", PlanSettings(daily=3), priority="holdings"))
    ad.fetch(**YEARLY)
    with pytest.raises(SourceUnavailable, match="UDAPI1207"):
        ad.fetch(**{**BALANCE, "resource": "cash_flow"})
    ad.fetch(**BALANCE)
    with pytest.raises(QuotaExceeded, match="upstox daily quota reached"):
        ad.fetch(**{**BALANCE, "resource": "share_holdings"})
    set_limit(conn, "upstox", daily=100)  # room appears, but the run is halted: no retry
    with pytest.raises(QuotaExceeded):
        ad.fetch(**YEARLY)
    assert vendor.calls == 3
    assert vendor_rows(conn) == [
        ("upstox", "income_statement", "ok"),
        ("upstox", "cash_flow", "error"),
        ("upstox", "balance_sheet", "ok"),
    ]


def test_a_rate_limited_request_settles_as_halted() -> None:
    conn, vendor = ledger(), UpstoxVendor()
    vendor.fail["income-statement"] = httpx.Response(429)
    ad = upstox_adapter(vendor)
    ad.use_budget(Budget(conn, "upstox", PlanSettings(daily=3)))
    with pytest.raises(RateLimited):
        ad.fetch(**YEARLY)
    assert vendor_rows(conn) == [("upstox", "income_statement", "halted")]


def test_a_broken_ledger_sends_nothing_and_halts() -> None:
    vendor = UpstoxVendor()
    budget = Budget(ledger(), "upstox", PlanSettings(daily=3))

    def locked(tool: str, symbol: str | None = None) -> int:
        raise sqlite3.OperationalError("database is locked")

    real = budget.reserve
    budget.reserve = locked  # type: ignore[method-assign]
    ad = upstox_adapter(vendor)
    ad.use_budget(budget)
    with pytest.raises(SourceUnavailable, match="quota ledger unavailable"):
        ad.fetch(**YEARLY)
    budget.reserve = real  # type: ignore[method-assign]  # the ledger recovers; the run stays halted
    with pytest.raises(SourceUnavailable, match="quota ledger unavailable"):
        ad.fetch(**BALANCE)
    assert vendor.calls == 0


def test_a_settle_error_does_not_lose_the_reply() -> None:
    vendor = UpstoxVendor()
    budget = Budget(ledger(), "upstox", PlanSettings(daily=3))

    def locked(call_id: int, outcome: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    budget.settle = locked  # type: ignore[method-assign]
    ad = upstox_adapter(vendor)
    ad.use_budget(budget)
    assert ad.fetch(**YEARLY).data["status"] == "success"  # counted; the outcome is advisory


# --- mappers: pure, on the hand-written fixtures ---


def statement_docs() -> dict[str, Any]:
    income = upstox_doc("income_statement")
    return {
        "income_yearly": income["yearly"], "income_quarterly": income["quarterly"],
        "balance_sheet": upstox_doc("balance_sheet"), "cash_flow": upstox_doc("cash_flow"),
    }  # fmt: skip


def stored(out: Any) -> dict[tuple[str, str, date], Decimal]:
    return {(r.item, r.period_type, r.period_end): r.value for r in out.statements}


def test_income_statement_gives_revenue_and_net_income_in_absolute_inr() -> None:
    got = stored(map_statements(statement_docs(), TODAY))
    crore = Decimal(10) ** 7
    assert got[("revenue", "A", FY)] == Decimal("1350.75") * crore
    assert got[("net_income", "A", FY_PRIOR)] == Decimal("150.25") * crore
    assert got[("revenue", "Q", date(2025, 12, 31))] == Decimal("340.25") * crore
    assert got[("net_income", "Q", date(2026, 6, 30))] == Decimal("44.75") * crore


def test_balance_sheet_gives_total_assets_and_equity_as_capital_plus_reserves() -> None:
    got = stored(map_statements(statement_docs(), TODAY))
    crore = Decimal(10) ** 7
    assert got[("total_assets", "A", FY)] == Decimal("2400.75") * crore
    assert got[("total_equity", "A", FY)] == Decimal("1017.75") * crore  # 67.5 + 950.25
    assert got[("total_equity", "A", FY_PRIOR)] == Decimal("800.25") * crore
    assert {k[0] for k in got} == {"revenue", "net_income", "total_assets", "total_equity", "cfo"}


def drop_line(docs: dict[str, Any], name: str, period: str | None = None) -> None:
    lines = docs["balance_sheet"]["data"]["full_statement"]
    for line in list(lines):
        if line["name"] == name and period is None:
            lines.remove(line)
        elif line["name"] == name:
            line["history"] = [h for h in line["history"] if h["period"] != period]


@pytest.mark.parametrize("missing", ["Equity Capital", "Reserves"])
def test_total_equity_needs_both_lines_else_it_is_one_skipped_line(missing: str) -> None:
    docs = statement_docs()
    drop_line(docs, missing)
    out = map_statements(docs, TODAY)
    assert not any(r.item == "total_equity" for r in out.statements)
    assert ("total_assets", "A", FY) in stored(out)  # the rest of the balance sheet is kept
    lines = [s for s in out.skipped if "total_equity" in s]
    assert len(lines) == 1 and "Equity Capital and Reserves" in lines[0]


def test_total_equity_is_skipped_per_period_when_only_one_period_lacks_a_line() -> None:
    docs = statement_docs()
    drop_line(docs, "Reserves", "Mar 2025")
    out = map_statements(docs, TODAY)
    got = stored(out)
    assert ("total_equity", "A", FY) in got and ("total_equity", "A", FY_PRIOR) not in got
    assert len([s for s in out.skipped if "total_equity" in s]) == 1


def test_a_balance_sheet_without_a_full_statement_has_no_equity_and_says_so() -> None:
    docs = statement_docs()
    del docs["balance_sheet"]["data"]["full_statement"]
    out = map_statements(docs, TODAY)
    assert not any(r.item == "total_equity" for r in out.statements)
    assert any("total_equity" in s for s in out.skipped)
    assert ("total_assets", "A", FY) in stored(out)


def test_cash_flow_gives_operating_cash_flow_only() -> None:
    out = map_statements({"cash_flow": upstox_doc("cash_flow")}, TODAY)
    assert {(r.item, r.period_type) for r in out.statements} == {("cfo", "A")}
    assert stored(out)[("cfo", "A", FY)] == Decimal("240.25") * Decimal(10) ** 7


def test_shareholding_gives_the_promoter_percentage_only() -> None:
    out = map_shareholding(upstox_doc("share_holdings"), TODAY)
    assert [(s.period_end, s.promoter_pct) for s in out.shareholding] == [
        (date(2025, 3, 31), Decimal("50.3")),
        (date(2025, 12, 31), Decimal("50.1")),
        (date(2026, 6, 30), Decimal("50.0")),
    ]
    assert all(s.public_pct is None and s.promoter_pledged_pct is None for s in out.shareholding)


def test_units_other_than_crore_are_a_skipped_line_and_nothing_is_stored() -> None:
    for units in ("lakh", None):
        docs = statement_docs()
        for key in docs:
            if units is None:
                del docs[key]["data"]["units_in"]
            else:
                docs[key]["data"]["units_in"] = units
        out = map_statements(docs, TODAY)
        assert out.statements == [] and len(out.skipped) == 4
        assert all("crore" in s for s in out.skipped)


def test_period_labels_reuse_period_end_and_the_ttm_pseudo_period_is_ignored() -> None:
    docs = statement_docs()
    docs["income_quarterly"]["data"]["history"].append(
        {"period": "FY2026", "revenue": 1.0, "net_profit": 1.0}
    )
    out = map_statements(docs, TODAY)
    ends = {r.period_end for r in out.statements if r.period_type == "Q"}
    assert ends == {date(2025, 12, 31), date(2026, 6, 30)}  # TTM ignored, FY2026 not understood
    unknown = [s for s in out.skipped if "not understood" in s]
    assert len(unknown) == 1 and "FY2026" in unknown[0] and "TTM" not in unknown[0]


def test_every_filing_date_is_an_estimate_with_a_documented_lag() -> None:
    assert (QUARTERLY_LAG, ANNUAL_LAG, SHAREHOLDING_LAG) == (
        timedelta(days=45), timedelta(days=60), timedelta(days=21),
    )  # fmt: skip
    out = map_statements(statement_docs(), TODAY)
    filed = {(r.period_type, r.period_end): r.filed_at for r in out.statements}
    assert filed[("A", FY)] == date(2026, 5, 30)  # Mar 31 + 60 days
    assert filed[("Q", date(2025, 12, 31))] == date(2026, 2, 14)  # + 45 days
    assert all(r.period_end <= r.filed_at <= TODAY for r in out.statements)
    held = map_shareholding(upstox_doc("share_holdings"), TODAY).shareholding
    assert held[0].filed_at == date(2025, 4, 21) and held[1].filed_at == date(2026, 1, 21)


def test_filing_dates_are_clamped_to_today_and_future_periods_are_skipped() -> None:
    today = date(2026, 4, 10)  # the Mar 2026 year ends before it, its +60 day estimate does not
    out = map_statements(statement_docs(), today)
    got = [r for r in out.statements if r.period_end == FY]
    assert got and all(r.filed_at == today for r in got) and out.clamped == len(got)
    assert not any(r.period_end > today for r in out.statements)
    assert any("end after" in s and "Jun 2026" in s for s in out.skipped)
    held = map_shareholding(upstox_doc("share_holdings"), date(2025, 12, 31))
    assert [s.period_end for s in held.shareholding] == [date(2025, 3, 31), date(2025, 12, 31)]
    assert held.shareholding[1].filed_at == date(2025, 12, 31) and held.clamped == 1
    assert any("end after" in s for s in held.skipped)


def test_a_cell_that_is_not_a_number_is_a_skipped_line_and_a_null_is_just_absent() -> None:
    docs = statement_docs()
    rows = docs["income_yearly"]["data"]["history"]
    rows[1].update(revenue="n/a", net_profit=None)
    docs["cash_flow"]["data"]["categories"][0]["history"][1]["value"] = True
    out = map_statements(docs, TODAY)
    got = stored(out)
    assert ("revenue", "A", FY) not in got and ("net_income", "A", FY) not in got
    assert ("cfo", "A", FY) not in got and ("revenue", "A", FY_PRIOR) in got
    assert not any(v == 0 for v in got.values())
    lines = " ".join(out.skipped)
    assert "revenue" in lines and "cfo" in lines and "net_income" not in lines


def test_a_shareholding_percentage_outside_0_to_100_or_not_a_number_is_skipped() -> None:
    doc = upstox_doc("share_holdings")
    history = doc["data"]["categories"][0]["history"]
    history[0]["value"], history[1]["value"] = 150.0, "fifty"
    out = map_shareholding(doc, TODAY)
    assert [s.period_end for s in out.shareholding] == [date(2026, 6, 30)]
    assert len(out.skipped) == 2


def test_a_null_promoter_cell_is_absent_without_a_line() -> None:
    doc = upstox_doc("share_holdings")
    doc["data"]["categories"][0]["history"][0]["value"] = None
    out = map_shareholding(doc, TODAY)
    assert [s.period_end for s in out.shareholding] == [date(2025, 12, 31), date(2026, 6, 30)]
    assert out.skipped == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["data"].update(history="rows"),
        lambda d: d["data"].update(history=[1, "x", None, ["Mar 2026"]]),
        lambda d: d["data"].update(history=[{"period": 7, "revenue": 1.0}]),
        lambda d: d["data"].update(history=[{"period": "Mar 2026", "revenue": {"v": 1}}]),
        lambda d: d.update(data=None),
    ],
)
def test_a_hostile_history_never_crashes_the_mapper(mutate: Any) -> None:
    docs = {"income_yearly": upstox_doc("income_statement")["yearly"]}
    mutate(docs["income_yearly"])
    out = map_statements(docs, TODAY)
    assert out.statements == []


def test_hostile_series_never_crash_the_other_mappers() -> None:
    for doc in (
        {"data": {"categories": "x"}},
        {"data": {"categories": [1, {"category": "promoters", "history": "x"}]}},
        {"data": {"categories": [{"category": "promoters", "history": [1, {"period": None}]}]}},
        {"data": None},
    ):
        assert map_shareholding(doc, TODAY).shareholding == []
    bad = {"units_in": "crore", "full_statement": [3, {"name": "Reserves", "history": 4}]}
    assert map_statements({"balance_sheet": {"data": bad}}, TODAY).statements == []
    cash = {"cash_flow": {"data": {"units_in": "crore", "categories": 3}}}
    assert map_statements(cash, TODAY).statements == []


# --- corporate actions: mapped, never ingested ---


def test_corporate_actions_map_dividend_bonus_and_split_and_skip_rights() -> None:
    actions, skipped = map_corporate_actions(upstox_doc("corporate_actions"), 7)
    assert [(a.kind, a.ex_date, a.ratio, a.amount) for a in actions] == [
        ("dividend", date(2025, 8, 14), None, Decimal("5.5")),
        ("bonus", date(2026, 1, 5), Decimal(1), None),
        ("split", date(2026, 2, 10), Decimal("0.2"), None),
    ]
    assert all(a.security_id == 7 and a.source == "upstox" for a in actions)
    assert len(skipped) == 1 and "Rights" in skipped[0]


@pytest.mark.parametrize(
    "action",
    [
        {"name": "Dividend", "expiry_date": "soon", "amount": 5.5},
        {"name": "Dividend", "expiry_date": "14 Aug 2025", "amount": "five"},
        {"name": "Dividend", "expiry_date": "14 Aug 2025", "amount": 0},
        {"name": "Bonus", "expiry_date": "05 Jan 2026", "ratio": "one for one"},
        {"name": "Split", "expiry_date": "10 Feb 2026", "ratio": "1:0"},
        {"name": "Merger", "expiry_date": "10 Feb 2026"},
        {"name": 7, "expiry_date": "10 Feb 2026"},
        "not an object",
    ],
)
def test_an_unusable_corporate_action_is_one_skipped_line(action: Any) -> None:
    actions, skipped = map_corporate_actions({"data": {"corporate_actions": [action]}}, 1)
    assert actions == [] and len(skipped) == 1


def test_corporate_actions_without_a_list_are_empty() -> None:
    assert map_corporate_actions({"data": {"corporate_actions": "x"}}, 1) == ([], [])
    assert map_corporate_actions({"data": None}, 1) == ([], [])


# --- the provider ---


def harness() -> Any:
    return next(h for h in HARNESSES if h.name == "upstox")


def context(world: World, today: date = TODAY) -> CollectContext:
    run = Run(world.duck, world.settings)
    return CollectContext(world.sec, world.master, today, run.get, run.failed, None)


def test_the_provider_declares_what_it_is_and_what_it_cannot_do() -> None:
    info = provider_class("upstox").info
    assert info.capabilities == {Capability.statements, Capability.shareholding, Capability.actions}
    assert (info.keyed_on, info.credential, info.metered) == ("isin", UPSTOX_CRED, False)
    assert info.sources == ("upstox",) and info.filing_dates == "estimated"
    for label in (info.revenue_basis, info.profit_basis):
        assert "consolidated unless the fixture says otherwise (UNVERIFIED)" in label
    assert Capability.pledge not in info.capabilities  # no pledge field in the documented reply


def test_collect_asks_five_endpoints_and_never_corporate_actions(world: World) -> None:
    vendor = UpstoxVendor()
    res = harness().provider(vendor).collect(context(world))
    assert vendor.endpoints == [
        "income-statement", "income-statement", "balance-sheet", "cash-flow", "share-holdings",
    ]  # fmt: skip
    assert [r.url.params.get("time_period") for r in vendor.requests[:2]] == ["yearly", "quarterly"]
    assert res.calls is None and res.supersede is None and res.shareholding_source is None
    assert [src for src, _ in res.groups] == ["upstox"] and res.statements


def test_every_row_is_counted_as_estimated_and_the_note_says_how(world: World) -> None:
    res = harness().provider(UpstoxVendor()).collect(context(world))
    rows = [r for _, rs in res.groups for r in rs]
    assert res.estimated == len(rows) > 0 and res.clamped == 0
    assert res.notes == [
        f"{len(rows)} of {len(rows)} rows carry an estimated filing date (Upstox gives none: "
        "statements +45 days quarterly and +60 days annual, shareholding +21 days; approximate)"
    ]


def test_clamped_dates_are_counted_in_a_note(world: World) -> None:
    res = harness().provider(UpstoxVendor()).collect(context(world, date(2026, 4, 10)))
    assert res.clamped > 0
    assert f"{res.clamped} filing dates after today were set to today" in res.notes


@pytest.mark.parametrize("isin", [None, "", "ine002a01018", "INE002A0101", "../balance-sheet"])
def test_no_valid_isin_is_not_configured_with_the_reason_and_never_guessed(
    world: World, isin: str | None
) -> None:
    world.sec = dataclasses.replace(world.sec, isin=isin)
    vendor = UpstoxVendor()
    with pytest.raises(NotConfigured, match="no valid ISIN") as e:
        harness().provider(vendor).collect(context(world))
    assert "RELIANCE" in str(e.value) and "nivesh master build" in str(e.value)
    assert vendor.calls == 0


def test_enabling_it_without_a_token_is_one_failed_line_not_a_crash(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(UPSTOX_CRED)
    vendor, ctx = UpstoxVendor(), context(world)
    res = harness().provider(vendor).collect(ctx)
    assert vendor.calls == 0 and not res.statements and not res.shareholding
    assert len(ctx.failed) == 1 and f"nivesh secrets set {UPSTOX_CRED}" in ctx.failed[0]


def test_one_failure_shared_by_every_endpoint_is_one_failed_line(world: World) -> None:
    world.sec = dataclasses.replace(world.sec, isin="INE111A01010")  # the double does not know it
    vendor, ctx = UpstoxVendor(), context(world)
    res = harness().provider(vendor).collect(ctx)
    assert vendor.calls == 5 and not res.statements
    assert len(ctx.failed) == 1 and "UDAPI1206" in ctx.failed[0]


def test_a_mapper_that_chokes_is_a_failed_line_not_a_crash(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def choke(*args: Any) -> Any:
        raise ValueError("choked")

    monkeypatch.setattr(fundamentals_upstox, "map_statements", choke)
    ctx = context(world)
    res = harness().provider(UpstoxVendor()).collect(ctx)
    assert not res.statements and res.shareholding  # the shareholding does not depend on it
    assert any("unusable Upstox data" in f and "choked" in f for f in ctx.failed)
    monkeypatch.setattr(fundamentals_upstox, "map_shareholding", choke)
    ctx = context(world)
    assert not harness().provider(UpstoxVendor()).collect(ctx).shareholding
    assert sum("unusable Upstox data" in f for f in ctx.failed) == 2  # statements and shareholding


def test_the_secret_name_and_pacing_come_from_the_spec(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(UPSTOX_CRED)
    monkeypatch.setenv("OTHER_CRED", upstox_cred())  # the default secret is not set at all
    made: list[RateLimiter] = []

    def limiter(per_sec: float) -> RateLimiter:
        made.append(RateLimiter(per_sec, lambda: 0.0, lambda s: None))
        return made[-1]

    monkeypatch.setattr(fundamentals_upstox, "RateLimiter", limiter)
    spec = ProviderSpec(
        name="upstox", credential="ref:OTHER_CRED", plan=PlanSettings(daily=50, min_interval_s=2.5)
    )
    vendor = UpstoxVendor()
    res = UpstoxProvider(spec, lambda: upstox_adapter(vendor)).collect(context(world))
    assert res.statements and [m.interval for m in made] == [pytest.approx(2.5)]
    default = UpstoxProvider(ProviderSpec(name="upstox"))
    assert default.credential_name == UPSTOX_CRED


def test_the_adapter_has_no_public_method_with_a_write_verb() -> None:
    assert write_methods(UpstoxFundamentals) == []  # the safety suite discovers it too
