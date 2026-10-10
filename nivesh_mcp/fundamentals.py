"""`nivesh-fundamentals`: read-only standardised statements, ratios, peers, analyst estimates and
shareholding (ST-4.10). Statements are point in time: `as_of` shows only what had been filed by
that date. Values are JSON numbers; a missing item is absent or null, never zero.
"""

from collections import defaultdict
from datetime import date
from typing import Any

from nivesh_core.market_store import (
    estimate_history,
    get_statement_rows,
    latest_estimates,
)
from nivesh_core.market_store import (
    get_shareholding as stored_shareholding,
)
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_engine.ratios import ratios
from nivesh_engine.statements import latest_as_of, revision
from nivesh_mcp import common
from nivesh_mcp.base import ReadOnlyServer
from nivesh_mcp.common import MarketCtx, env, num, page, parse_day, resolve_one

server = ReadOnlyServer("fundamentals")
SOURCE = "nivesh-fundamentals"
MAX_YEARS = 10
PERIOD_TYPES = ("A", "Q")


def _pick(m: MarketCtx, query: str) -> SecurityRow:
    master = SecurityMaster(m.sql)
    row = master.get(resolve_one(master, query))
    if row is None:  # unreachable: the id came from the same table
        raise ValueError(f"no security matches {query!r}")
    return row


def _head(sec: SecurityRow) -> dict[str, Any]:
    return {"security_id": sec.id, "symbol": sec.symbol, "exchange": sec.exchange,
            "isin": sec.isin}  # fmt: skip


def _as_of(value: str | None) -> date | None:
    return parse_day(value, "as_of") if value else None


@server.tool
def get_statements(
    security: str,
    period_type: str = "A",
    years: int = 5,
    as_of: str | None = None,
    limit: int = 500,
    cursor: int = 0,
) -> dict[str, Any]:
    """Standardised statement values per period (newest first): period_type A (annual) or Q
    (quarterly), the last `years` years (at most 10). Each period lists its period_end, the
    latest filed_at and values by item. `as_of` (ISO date) hides anything filed after it."""
    if period_type not in PERIOD_TYPES:
        raise ValueError("period_type must be A (annual) or Q (quarterly)")
    cutoff = _as_of(as_of)
    keep = max(1, min(years, MAX_YEARS)) * (1 if period_type == "A" else 4)
    with common.market_ctx() as m:
        sec = _pick(m, security)
        rows = [r for r in latest_as_of(get_statement_rows(m.duck, sec.id), cutoff)
                if r.period_type == period_type]  # fmt: skip
    by_end: dict[date, list[Any]] = defaultdict(list)
    for r in rows:
        by_end[r.period_end].append(r)
    periods = [
        {
            "period_end": end.isoformat(), "period_type": period_type,
            "currency": next((r.currency for r in rs if r.currency != "pct"), rs[0].currency),
            "filed_at": max(r.filed_at for r in rs).isoformat(),
            "values": {r.item: num(r.value) for r in sorted(rs, key=lambda r: r.item)},
        }
        for end, rs in sorted(by_end.items(), reverse=True)[:keep]
    ]  # fmt: skip
    chunk, nxt = page(periods, limit, cursor)
    data = {**_head(sec), "periods": chunk, "total": len(periods)}
    return env(data, max((r.filed_at for r in rows), default=None), SOURCE, next_cursor=nxt)


@server.tool
def get_ratios(security: str, period_type: str = "A", as_of: str | None = None) -> dict[str, Any]:
    """Operating and net margin, return on equity, debt to equity and revenue growth for the
    latest period known at `as_of`. A ratio with missing inputs is null and the inputs are
    listed under `missing`."""
    if period_type not in PERIOD_TYPES:
        raise ValueError("period_type must be A (annual) or Q (quarterly)")
    cutoff = _as_of(as_of)
    with common.market_ctx() as m:
        sec = _pick(m, security)
        rows = get_statement_rows(m.duck, sec.id)
    rs = ratios(rows, period_type, cutoff)  # type: ignore[arg-type]
    data = {
        **_head(sec),
        "period_end": rs.period_end.isoformat() if rs.period_end else None,
        "period_type": period_type,
        "ratios": {k: num(v) for k, v in rs.values.items()},
        "missing": rs.missing,
    }
    return env(data, rs.period_end, SOURCE)


@server.tool
def get_peers(security: str, limit: int = 20) -> dict[str, Any]:
    """Other listed securities in the same industry and market, sorted by symbol. Industry data
    exists only for some listings, so the list can be empty (the reason is then given)."""
    with common.market_ctx() as m:
        sec = _pick(m, security)
        found = SecurityMaster(m.sql).peers(sec.id, limit=max(1, min(limit, common.MAX_ROWS)))
    if found.reason:
        data = {**_head(sec), "industry": None, "peers": [], "reason": found.reason}
        return env(data, None, SOURCE)
    peers = [
        {"security_id": r.id, "symbol": r.symbol, "exchange": r.exchange, "name": r.name}
        for r in found.rows
    ]
    return env({**_head(sec), "industry": sec.industry, "peers": peers}, None, SOURCE)


@server.tool
def get_estimates(security: str) -> dict[str, Any]:
    """Latest analyst revenue and EPS estimates with 30 and 90 day revision direction (up, down,
    flat or null when history is shorter than the window). India has no free source: the result
    is marked unavailable with a reason, never zero."""
    with common.market_ctx() as m:
        sec = _pick(m, security)
        if sec.market != "US":
            reason = "no free estimates source for India (deferred)"
            return env({**_head(sec), "available": False, "reason": reason, "estimates": []},
                       None, SOURCE)  # fmt: skip
        latest = latest_estimates(m.duck, sec.id)
        hist = {(e.metric, e.period): estimate_history(m.duck, sec.id, e.metric, e.period)
                for e in latest}  # fmt: skip
    if not latest:
        reason = "no estimates stored; run `nivesh market estimates`"
        return env({**_head(sec), "available": False, "reason": reason, "estimates": []},
                   None, SOURCE)  # fmt: skip
    newest = max(e.as_of for e in latest)
    rows = [
        {
            "metric": e.metric, "period": e.period, "value": num(e.value),
            "as_of": e.as_of.isoformat(),
            "revision_30d": revision(hist[(e.metric, e.period)], newest, 30),
            "revision_90d": revision(hist[(e.metric, e.period)], newest, 90),
        }
        for e in latest
    ]  # fmt: skip
    return env({**_head(sec), "available": True, "estimates": rows}, newest, SOURCE)


@server.tool
def get_shareholding(security: str, quarters: int = 12) -> dict[str, Any]:
    """Quarterly promoter, pledged-promoter and public shareholding percentages (newest first),
    with the filing date. Available for India listings only; empty otherwise."""
    with common.market_ctx() as m:
        sec = _pick(m, security)
        found = stored_shareholding(m.duck, sec.id)
    rows = [
        {
            "period_end": s.period_end.isoformat(), "promoter_pct": num(s.promoter_pct),
            "promoter_pledged_pct": num(s.promoter_pledged_pct), "public_pct": num(s.public_pct),
            "filed_at": s.filed_at.isoformat(),
        }
        for s in reversed(found)
    ][: max(1, min(quarters, 40))]  # fmt: skip
    return env(
        {**_head(sec), "quarters": rows}, max((s.filed_at for s in found), default=None), SOURCE
    )
