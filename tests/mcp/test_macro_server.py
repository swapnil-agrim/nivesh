from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastmcp.exceptions import ToolError

from nivesh_core.market_store import write_macro
from nivesh_core.pii_scan import scan_text
from nivesh_mcp.base import is_write_name
from nivesh_mcp.registry import SERVERS
from tests.mcp.mkt_fx import Mkt, call

D = Decimal
TOOLS = ["get_series", "get_flows_india", "get_rates_snapshot"]


def put(m: Mkt, role: str, pts: list[tuple[str, str]], source: str = "fred") -> None:
    with m.duck() as c:
        write_macro(c, role, [(date.fromisoformat(d), D(v)) for d, v in pts], source)


def test_tools_exact_set() -> None:
    assert SERVERS["macro"].tool_names == TOOLS
    assert not [t for t in TOOLS if is_write_name(t)]


async def test_get_series_dated_values_paginated_and_capped(mkt: Mkt) -> None:
    start = date(2024, 1, 1)
    put(mkt, "y10_us", [((start + timedelta(days=i)).isoformat(), str(4 + i / 1000))
                        for i in range(620)])  # fmt: skip
    got, cursor = [], 0
    while True:
        out = await call("macro", "get_series", series_id="y10_us", start="2024-01-01",
                         end="2030-01-01", limit=100000, cursor=cursor)  # fmt: skip
        assert len(out["data"]["points"]) <= 500
        got += out["data"]["points"]
        if out.get("next_cursor") is None:
            break
        cursor = out["next_cursor"]
    assert len(got) == 620 and got[0] == {"date": "2024-01-01", "value": 4.0}
    assert out["data"]["series_id"] == "y10_us" and out["data"]["total"] == 620
    ranged = await call("macro", "get_series", series_id="y10_us", start="2024-01-10",
                        end="2024-01-12")  # fmt: skip
    assert [p["date"] for p in ranged["data"]["points"]] == [
        "2024-01-10",
        "2024-01-11",
        "2024-01-12",
    ]
    assert ranged["as_of"] == "2024-01-12"


async def test_get_series_unknown_id_lists_configured_ids(mkt: Mkt) -> None:
    with pytest.raises(ToolError, match="unknown series_id 'nope'.*usdinr.*y10_us"):
        await call("macro", "get_series", series_id="nope")
    with pytest.raises(ToolError, match="after end"):
        await call("macro", "get_series", series_id="usdinr", start="2026-02-01", end="2026-01-01")
    empty = await call("macro", "get_series", series_id="usdinr")  # configured, nothing stored yet
    assert empty["data"]["points"] == [] and empty["data"]["total"] == 0


async def test_usdinr_daily_reference_available_via_get_series(mkt: Mkt) -> None:
    put(mkt, "usdinr", [("2026-01-02", "90.1234"), ("2026-01-05", "90.25")])
    out = await call("macro", "get_series", series_id="usdinr")
    assert [p["value"] for p in out["data"]["points"]] == [90.1234, 90.25]
    assert out["data"]["series_id"] == "usdinr" and out["data"]["source"] == "fred"


async def test_get_flows_india_window_returns_fii_dii_net(mkt: Mkt) -> None:
    fii = [("2025-12-01", "-100.5"), ("2026-01-02", "-1500.5"), ("2026-01-05", "300")]
    put(mkt, "fii_net", fii, "nse_flows")
    put(mkt, "dii_net", [("2026-01-02", "1345.57"), ("2026-01-05", "200")], "nse_flows")
    out = await call("macro", "get_flows_india", window_days=7)
    rows = out["data"]["days"]
    assert [r["date"] for r in rows] == ["2026-01-05", "2026-01-02"]  # newest first, window only
    assert rows[1] == {"date": "2026-01-02", "fii_net": -1500.5, "dii_net": 1345.57}
    assert out["data"]["totals"] == {"fii_net": -1200.5, "dii_net": 1545.57}
    assert out["as_of"] == "2026-01-05"
    wide = await call("macro", "get_flows_india", window_days=60)
    assert len(wide["data"]["days"]) == 3 and wide["data"]["days"][2]["dii_net"] is None


async def test_get_rates_snapshot_returns_policy_rates_10y_cpi_yoy_and_last_change_dates(
    mkt: Mkt,
) -> None:
    put(mkt, "policy_in", [("2025-10-01", "5.5"), ("2025-12-01", "5.25"), ("2026-01-01", "5.25")])
    put(mkt, "policy_us", [("2025-12-30", "4.5"), ("2026-01-05", "4.25")])
    put(mkt, "y10_in", [("2025-12-01", "6.5")])
    put(mkt, "y10_us", [("2026-01-05", "4.1")])
    put(mkt, "cpi_us", [("2024-12-01", "300"), ("2025-12-01", "309")])
    out = await call("macro", "get_rates_snapshot")
    roles = out["data"]["roles"]
    assert roles["policy_in"]["value"] == 5.25 and roles["policy_in"]["last_change"] == "2025-12-01"
    assert roles["policy_us"]["last_change"] == "2026-01-05" and roles["y10_us"]["value"] == 4.1
    assert roles["cpi_us"]["yoy_pct"] == pytest.approx(3.0) and roles["cpi_us"]["value"] == 309.0
    assert roles["y10_us"]["stale"] is False and out["data"]["as_of_date"] == "2026-01-05"


async def test_rates_snapshot_marks_missing_role_unavailable_not_zero(mkt: Mkt) -> None:
    put(mkt, "y10_us", [("2026-01-05", "4.1")])
    roles = (await call("macro", "get_rates_snapshot"))["data"]["roles"]
    cpi = roles["cpi_in"]
    assert cpi["available"] is False and cpi["value"] is None and cpi["reason"]
    assert roles["y10_us"]["available"] is True
    put(mkt, "usdinr", [("2025-12-20", "90")])  # more than five days old
    assert (await call("macro", "get_rates_snapshot"))["data"]["roles"]["usdinr"]["stale"] is True


async def test_payload_survives_redaction(mkt: Mkt) -> None:
    put(mkt, "crude", [("2026-01-05", "61.37")])
    put(mkt, "fii_net", [("2026-01-05", "-123456789012.5")], "nse_flows")
    out = await call("macro", "get_flows_india", window_days=7)
    assert out["data"]["days"][0]["fii_net"] == -123456789012.5
    snap = await call("macro", "get_rates_snapshot")
    assert snap["data"]["roles"]["crude"]["value"] == 61.37
    assert "[REDACTED]" not in str(out) + str(snap) and scan_text(str(out) + str(snap)) == []
