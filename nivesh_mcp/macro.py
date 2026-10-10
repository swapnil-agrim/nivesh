"""`nivesh-macro`: read-only macro series, India institutional flows and a rates snapshot
(ST-4.10). Series are stored under their configured role names (`y10_us`, `usdinr`, ...).
Numbers are JSON numbers; a role with no data is reported unavailable, never zero.
"""

from datetime import timedelta
from typing import Any

from nivesh_core.market_store import get_macro
from nivesh_core.timeutil import ist_date
from nivesh_engine.macro import Obs, rates_snapshot
from nivesh_mcp import common
from nivesh_mcp.base import ReadOnlyServer
from nivesh_mcp.common import env, num, page, parse_day

server = ReadOnlyServer("macro")
SOURCE = "nivesh-macro"
MAX_WINDOW_DAYS = 366
FLOW_ROLES = ("fii_net", "dii_net")
SNAPSHOT_HISTORY_DAYS = 800  # enough for a year-on-year comparison on monthly series


@server.tool
def get_series(
    series_id: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
    cursor: int = 0,
) -> dict[str, Any]:
    """Dated values of a configured macro series (a role such as policy_us, y10_in, cpi_us,
    usdinr, vix_us, crude) between optional ISO dates, oldest first. An unknown id lists the
    configured ids."""
    first = parse_day(start, "start") if start else None
    last = parse_day(end, "end") if end else None
    if first and last and first > last:
        raise ValueError(f"start {first} is after end {last}")
    cfg = common.settings().market.macro_series
    if series_id not in cfg:
        raise ValueError(
            f"unknown series_id {series_id!r}; configured ids: {', '.join(sorted(cfg))}"
        )
    with common.market_ctx() as m:
        points = get_macro(m.duck, series_id, first, last)
    chunk, nxt = page(points, limit, cursor)
    data = {
        "series_id": series_id, "source": cfg[series_id].source,
        "points": [{"date": d.isoformat(), "value": num(v)} for d, v in chunk],
        "total": len(points),
    }  # fmt: skip
    return env(data, points[-1][0] if points else None, SOURCE, next_cursor=nxt)


@server.tool
def get_flows_india(window_days: int = 30) -> dict[str, Any]:
    """FII and DII net flows in rupees crore per day over the last `window_days` days (at most
    366), newest first, with window totals."""
    today = ist_date(common.now())
    first = today - timedelta(days=max(1, min(window_days, MAX_WINDOW_DAYS)))
    with common.market_ctx() as m:
        series = {r: dict(get_macro(m.duck, r, first, today)) for r in FLOW_ROLES}
    days = sorted(set().union(*(set(s) for s in series.values())), reverse=True)
    rows = [{"date": d.isoformat(), **{r: num(series[r].get(d)) for r in FLOW_ROLES}} for d in days]
    totals = {r: float(sum(series[r].values())) for r in FLOW_ROLES}
    return env({"days": rows, "totals": totals, "unit": "INR crore"}, days[0] if days else None,
               SOURCE)  # fmt: skip


@server.tool
def get_rates_snapshot() -> dict[str, Any]:
    """Latest policy rates, 10 year yields, CPI with year-on-year change, USDINR, VIX and crude,
    with the date each was observed, the last policy-rate change date and a stale flag. A role
    with no stored data is marked unavailable with a reason."""
    today = ist_date(common.now())
    with common.market_ctx() as m:
        first = today - timedelta(days=SNAPSHOT_HISTORY_DAYS)
        series: dict[str, list[Obs]] = {
            role: [Obs(d, v) for d, v in get_macro(m.duck, role, first, today)]
            for role in common.settings().market.macro_series
        }
    snap = rates_snapshot(series, today)
    roles = {
        r: {
            "available": s.available, "value": num(s.value),
            "observed": s.as_of.isoformat() if s.as_of else None,
            "last_change": s.last_change.isoformat() if s.last_change else None,
            "yoy_pct": num(s.yoy_pct), "stale": s.stale, "reason": s.reason,
        }
        for r, s in snap.items()
    }  # fmt: skip
    seen = [s.as_of for s in snap.values() if s.as_of]
    return env({"roles": roles, "as_of_date": max(seen).isoformat() if seen else None},
               max(seen) if seen else None, SOURCE)  # fmt: skip
