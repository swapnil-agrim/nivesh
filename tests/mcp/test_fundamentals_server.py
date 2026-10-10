from datetime import date
from decimal import Decimal

import pytest
from fastmcp.exceptions import ToolError

from nivesh_core.market_models import ShareholdingRow
from nivesh_core.market_store import write_estimates, write_fundamentals
from nivesh_core.pii_scan import scan_text
from nivesh_engine.statements import StatementRow
from nivesh_mcp.base import is_write_name
from nivesh_mcp.registry import SERVERS
from tests.mcp.mkt_fx import Mkt, call

D = Decimal
TOOLS = ["get_statements", "get_ratios", "get_peers", "get_estimates", "get_shareholding"]


def srow(item: str, value: str, end: str, ptype: str = "A", filed: str | None = None,
         cur: str = "INR") -> StatementRow:  # fmt: skip
    e = date.fromisoformat(end)
    return StatementRow(
        period_end=e,
        period_type=ptype,
        item=item,
        value=D(value),  # type: ignore[arg-type]
        currency=cur,
        filed_at=date.fromisoformat(filed or f"{e.year + 1}-02-01"),
    )


def seed_reliance(m: Mkt) -> int:
    sid = m.ids["RELIANCE"]
    rows = []
    for y in range(2019, 2025):  # six annual periods
        rows += [srow("revenue", str(1000 + 100 * (y - 2019)), f"{y}-03-31"),
                 srow("net_income", str(100 + 10 * (y - 2019)), f"{y}-03-31"),
                 srow("total_equity", "500", f"{y}-03-31"),
                 srow("total_debt", "250", f"{y}-03-31"),
                 srow("operating_income", "200", f"{y}-03-31")]  # fmt: skip
    for q in range(1, 13):  # twelve quarters
        end = (
            f"{2022 + (q - 1) // 4}-{[6, 9, 12, 3][(q - 1) % 4]:02d}-"
            + ("30", "30", "31", "31")[(q - 1) % 4]
        )
        rows.append(srow("revenue", str(250 + q), end, "Q", filed="2025-06-01"))
    rows.append(srow("revenue", "1400", "2024-03-31", filed="2025-09-01"))  # restated, filed later
    with m.duck() as c:
        write_fundamentals(c, sid, rows, "bse_xbrl")
    return sid


def test_tools_exact_set() -> None:
    assert SERVERS["fundamentals"].tool_names == TOOLS
    assert not [t for t in TOOLS if is_write_name(t)]


async def test_get_statements_annual_quarterly_filed_at_and_point_in_time(mkt: Mkt) -> None:
    seed_reliance(mkt)
    ann = await call("fundamentals", "get_statements", security="RELIANCE")
    periods = ann["data"]["periods"]
    assert [p["period_end"] for p in periods][:2] == ["2024-03-31", "2023-03-31"]  # newest first
    assert (
        len(periods) == 5 and periods[0]["period_type"] == "A" and periods[0]["currency"] == "INR"
    )
    assert periods[0]["values"]["revenue"] == 1400.0 and periods[0]["filed_at"] == "2025-09-01"
    q = await call("fundamentals", "get_statements", security="RELIANCE", period_type="Q", years=3)
    assert len(q["data"]["periods"]) == 12 and q["data"]["periods"][0]["period_type"] == "Q"
    pit = await call("fundamentals", "get_statements", security="RELIANCE", as_of="2025-03-01")
    assert pit["data"]["periods"][0]["values"]["revenue"] == 1500.0  # before the restatement
    assert pit["data"]["periods"][0]["filed_at"] == "2025-02-01"


async def test_get_statements_years_cap_and_pagination(mkt: Mkt) -> None:
    seed_reliance(mkt)
    out = await call("fundamentals", "get_statements", security="RELIANCE", years=100, limit=2)
    assert len(out["data"]["periods"]) == 2 and out["next_cursor"] == 2
    assert out["data"]["total"] == 6  # 100 years is capped to 10; only six exist
    nxt = await call("fundamentals", "get_statements", security="RELIANCE", years=100, limit=2,
                     cursor=out["next_cursor"])  # fmt: skip
    assert nxt["data"]["periods"][0]["period_end"] == "2022-03-31"
    with pytest.raises(ToolError, match="period_type"):
        await call("fundamentals", "get_statements", security="RELIANCE", period_type="X")
    with pytest.raises(ToolError, match="as_of must be an ISO date"):
        await call("fundamentals", "get_statements", security="RELIANCE", as_of="soon")


async def test_bank_fields_present_only_for_banks(mkt: Mkt) -> None:
    bank, other = mkt.ids["HDFCBANK"], mkt.ids["TCS"]
    bank_rows = [
        srow("revenue", "900", "2025-03-31"),
        srow("gnpa_pct", "1.24", "2025-03-31", cur="pct"),
    ]
    with mkt.duck() as c:
        write_fundamentals(c, bank, bank_rows, "bse_xbrl")
        write_fundamentals(c, other, [srow("revenue", "900", "2025-03-31")], "bse_xbrl")
    b = await call("fundamentals", "get_statements", security="HDFCBANK")
    t = await call("fundamentals", "get_statements", security="TCS")
    assert b["data"]["periods"][0]["values"]["gnpa_pct"] == 1.24
    assert "gnpa_pct" not in t["data"]["periods"][0]["values"]


async def test_get_ratios_from_stored_statements_and_lists_missing_inputs(mkt: Mkt) -> None:
    seed_reliance(mkt)
    out = await call("fundamentals", "get_ratios", security="RELIANCE")
    d = out["data"]
    assert d["period_end"] == "2024-03-31" and d["period_type"] == "A" and d["missing"] == []
    assert d["ratios"]["operating_margin"] == pytest.approx(200 / 1400, abs=1e-4)
    assert d["ratios"]["revenue_growth"] is not None
    with mkt.duck() as c:
        write_fundamentals(c, mkt.ids["TCS"], [srow("revenue", "10", "2025-03-31")], "bse_xbrl")
    thin = await call("fundamentals", "get_ratios", security="TCS")
    assert thin["data"]["ratios"]["roe"] is None and "net_income" in thin["data"]["missing"]
    none = await call("fundamentals", "get_ratios", security="ONGC")
    assert none["data"]["period_end"] is None and none["data"]["ratios"]["roe"] is None


async def test_get_peers_same_industry_excludes_self_and_caps(mkt: Mkt) -> None:
    out = await call("fundamentals", "get_peers", security="RELIANCE")
    assert [p["symbol"] for p in out["data"]["peers"]] == ["ONGC"]
    assert out["data"]["industry"] == "Refineries"
    capped = await call("fundamentals", "get_peers", security="RELIANCE", limit=0)
    assert len(capped["data"]["peers"]) == 1  # limit clamps to at least one
    none = await call("fundamentals", "get_peers", security="AAPL")
    assert none["data"]["peers"] == [] or none["data"]["industry"] == "Consumer Electronics"
    bare = await call("fundamentals", "get_peers", security="TATAMOTORS")
    assert bare["data"]["peers"] == [] and bare["data"]["reason"]


async def test_get_estimates_us_returns_values_and_revisions(mkt: Mkt) -> None:
    sid = mkt.ids["AAPL"]
    snaps = (("2025-10-01", "7.0"), ("2025-12-05", "7.4"), ("2026-01-05", "7.5"))
    with mkt.duck() as c:
        for day, eps in snaps:
            vals = [("eps", "2026-09-30", D(eps)), ("revenue", "2026-09-30", D("100"))]
            write_estimates(c, sid, vals, date.fromisoformat(day), "fmp")
    out = await call("fundamentals", "get_estimates", security="AAPL")
    d = out["data"]
    assert d["available"] is True and out["as_of"] == "2026-01-05"
    by = {e["metric"]: e for e in d["estimates"]}
    assert by["eps"]["value"] == 7.5 and by["eps"]["revision_30d"] == "up"
    assert by["eps"]["revision_90d"] == "up" and by["revenue"]["revision_90d"] == "flat"


async def test_get_estimates_india_returns_available_false_with_reason_never_zero(mkt: Mkt) -> None:
    out = await call("fundamentals", "get_estimates", security="RELIANCE")
    d = out["data"]
    assert d["available"] is False and "India" in d["reason"] and d["estimates"] == []
    us = await call("fundamentals", "get_estimates", security="AAPL")  # nothing stored yet
    assert us["data"]["available"] is False and "nivesh market estimates" in us["data"]["reason"]


async def test_revision_unavailable_with_short_history(mkt: Mkt) -> None:
    with mkt.duck() as c:
        write_estimates(
            c, mkt.ids["AAPL"], [("eps", "2026-09-30", D("7.5"))], date(2026, 1, 5), "fmp"
        )
    by = (await call("fundamentals", "get_estimates", security="AAPL"))["data"]["estimates"][0]
    assert by["revision_30d"] is None and by["revision_90d"] is None


async def test_get_shareholding_pledge_per_quarter(mkt: Mkt) -> None:
    sid = mkt.ids["RELIANCE"]
    sh = [
        ShareholdingRow(period_end=date(2025, 9, 30), promoter_pct=D("50.11"),
                        promoter_pledged_pct=D("0"), public_pct=D("49.89"),
                        filed_at=date(2025, 10, 18)),
        ShareholdingRow(period_end=date(2025, 12, 31), promoter_pct=D("50.07"),
                        promoter_pledged_pct=D("1.25"), public_pct=None,
                        filed_at=date(2026, 1, 19)),
    ]  # fmt: skip
    with mkt.duck() as c:
        write_fundamentals(c, sid, [], "bse_xbrl", shareholding=sh)
    out = await call("fundamentals", "get_shareholding", security="RELIANCE")
    q = out["data"]["quarters"]
    assert [x["period_end"] for x in q] == ["2025-12-31", "2025-09-30"]  # newest first
    assert q[0]["promoter_pledged_pct"] == 1.25 and q[0]["public_pct"] is None
    assert q[1]["promoter_pledged_pct"] == 0.0 and out["as_of"] == "2026-01-19"
    assert (await call("fundamentals", "get_shareholding", security="TCS"))["data"][
        "quarters"
    ] == []


async def test_payload_redaction_and_pii_scan(mkt: Mkt) -> None:
    seed_reliance(mkt)
    with mkt.duck() as c:
        write_fundamentals(
            c, mkt.ids["TCS"], [srow("revenue", "123456789012", "2025-03-31")], "bse_xbrl"
        )
    big = await call("fundamentals", "get_statements", security="TCS")
    assert big["data"]["periods"][0]["values"]["revenue"] == 123456789012.0  # numbers survive
    out = await call(
        "fundamentals", "get_statements", security="RELIANCE", period_type="Q", years=3
    )
    assert "[REDACTED]" not in str(out) and scan_text(str(out)) == []


async def test_edgar_companyfacts_statements_and_ratios_have_no_share_only_period(
    mkt: Mkt,
) -> None:
    import json
    from pathlib import Path

    from nivesh_engine.statements import facts_from_companyfacts, quarterise

    fx = Path(__file__).resolve().parents[1] / "fixtures/market/edgar_companyfacts.json"
    rows = [r for r in quarterise(facts_from_companyfacts(json.loads(fx.read_text()))).rows
            if r.currency != "EUR"]  # fmt: skip
    with mkt.duck() as c:
        write_fundamentals(c, mkt.ids["RELIANCE"], rows, "edgar")
    for ptype in ("A", "Q"):
        out = await call("fundamentals", "get_statements", security="RELIANCE", period_type=ptype)
        periods = out["data"]["periods"]
        assert [p["period_end"] for p in periods] == ["2024-09-28"]
        assert "revenue" in periods[0]["values"]
        r = await call("fundamentals", "get_ratios", security="RELIANCE", period_type=ptype)
        assert r["data"]["period_end"] == "2024-09-28"
        got = r["data"]["ratios"]
        assert got["debt_to_equity"] is not None  # the fixture's quarter has balance sheet only
        if ptype == "A":
            assert all(got[k] is not None for k in ("operating_margin", "net_margin", "roe"))
