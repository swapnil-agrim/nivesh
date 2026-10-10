"""`nivesh-market`: read-only end-of-day prices, indices, corporate actions, security lookup and
the last completed trading session (ST-4.10).

Lists are sorted by date, oldest first, and paged with `limit` (at most 500) and `cursor`.
Numbers are JSON numbers; a `security` argument is any ISIN, symbol or company name.
"""

from typing import Any

from nivesh_adapters.market_ingest import calendar_for
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import get_bars, get_corp_actions
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_engine.adjust import adjust_closes
from nivesh_engine.calendar import last_trading_day
from nivesh_mcp import common
from nivesh_mcp.base import ReadOnlyServer
from nivesh_mcp.common import MarketCtx, env, num, page, parse_day, resolve_one

server = ReadOnlyServer("market")
SOURCE = "nivesh-market"
MARKETS = {"NSE": "IN", "NYSE": "US"}


def _security(row: SecurityRow) -> dict[str, Any]:
    return {
        "security_id": row.id, "symbol": row.symbol, "exchange": row.exchange, "name": row.name,
        "isin": row.isin, "currency": row.currency, "market": row.market,
        "asset_class": row.asset_class,
    }  # fmt: skip


def _bar(b: PriceBar) -> dict[str, Any]:
    return {
        "date": b.date.isoformat(), "open": num(b.open), "high": num(b.high), "low": num(b.low),
        "close": num(b.close), "volume": b.volume, "adj_close": num(b.adj_close),
        "source": b.source, "flag": b.flag,
    }  # fmt: skip


def _pick(m: MarketCtx, query: str) -> SecurityRow:
    row = SecurityMaster(m.sql).get(resolve_one(SecurityMaster(m.sql), query))
    if row is None:  # unreachable: the id came from the same table
        raise ValueError(f"no security matches {query!r}")
    return row


def _range(start: str, end: str) -> tuple[Any, Any]:
    first, last = parse_day(start, "start"), parse_day(end, "end")
    if first > last:
        raise ValueError(f"start {first} is after end {last}")
    return first, last


def _prices(
    m: MarketCtx, sec: SecurityRow, start: str, end: str, limit: int, cursor: int
) -> dict[str, Any]:
    first, last = _range(start, end)
    bars = get_bars(m.duck, sec.id, first, last)
    rows, nxt = page(bars, limit, cursor)
    data = {**_security(sec), "bars": [_bar(b) for b in rows], "total": len(bars)}
    return env(data, bars[-1].date if bars else None, SOURCE, next_cursor=nxt)


@server.tool
def get_prices(
    security: str, start: str, end: str, limit: int = 500, cursor: int = 0
) -> dict[str, Any]:
    """Daily OHLCV bars with split/dividend-adjusted close, source and cross-check flag for an
    ISIN, symbol or name between two ISO dates (oldest first). Pages of at most 500 bars."""
    with common.market_ctx() as m:
        return _prices(m, _pick(m, security), start, end, limit, cursor)


@server.tool
def get_index(name: str, start: str, end: str, limit: int = 500, cursor: int = 0) -> dict[str, Any]:
    """Daily bars for a market index such as NIFTY 50 or INDIA VIX (oldest first). An unknown
    name lists the known index names."""
    with common.market_ctx() as m:
        rows = m.sql.execute(
            "SELECT id, symbol, exchange, name, isin, currency, asset_class, market, sector, "
            "industry FROM security WHERE asset_class = 'index' AND unresolved = 0 ORDER BY symbol"
        ).fetchall()
        indices = [SecurityRow(*r) for r in rows]
        want = " ".join(name.upper().split())
        sec = next((i for i in indices if i.symbol.upper() == want), None)
        if sec is None:
            known = ", ".join(i.symbol for i in indices) or "none (run `nivesh master build`)"
            raise ValueError(f"unknown index {name!r}; known indices: {known}")
        return _prices(m, sec, start, end, limit, cursor)


def _split_adjusted(bars: list[PriceBar], actions: list[Any]) -> dict[Any, Any]:
    """Date -> split-adjusted close. Exchange closes are raw, so they are adjusted by the stored
    splits and bonuses; Yahoo closes already are, so they are left alone (never counted twice)."""
    out: dict[Any, Any] = {}
    for source in {b.source for b in bars}:
        mine = [b for b in bars if b.source == source]
        for b in adjust_closes(mine, actions, dividends=False, splits=source != "yahoo"):
            out[b.date] = b.adj_close
    return out


@server.tool
def get_quote_eod(security: str) -> dict[str, Any]:
    """The latest stored end-of-day bar with the previous close and percent change. Both are on
    a split-adjusted basis (previous_close_basis), so a split or bonus is not a price drop."""
    with common.market_ctx() as m:
        sec = _pick(m, security)
        bars = get_bars(m.duck, sec.id)
        acts = get_corp_actions(m.duck, sec.id) if len(bars) > 1 else []
    if not bars:
        data = {**_security(sec), "date": None, "close": None,
                "reason": "no prices stored; run `nivesh market prices`"}  # fmt: skip
        return env(data, None, SOURCE)
    last = bars[-1]
    prev = change = None
    if len(bars) > 1:
        adj = _split_adjusted(bars[-2:], acts)
        prev, now_ = adj[bars[-2].date], adj[last.date]
        change = None if prev == 0 else float((now_ - prev) / prev * 100)
    data = {**_security(sec), **_bar(last), "previous_close": num(prev),
            "previous_close_basis": "split_adjusted", "change_pct": change}  # fmt: skip
    return env(data, last.date, SOURCE)


@server.tool
def get_corporate_actions(security: str, since: str | None = None) -> dict[str, Any]:
    """Splits, bonuses and dividends on or after an optional ISO date (oldest first)."""
    start = parse_day(since, "since") if since else None
    with common.market_ctx() as m:
        sec = _pick(m, security)
        acts = get_corp_actions(m.duck, sec.id, start)
    rows = [
        {"ex_date": a.ex_date.isoformat(), "kind": a.kind, "ratio": num(a.ratio),
         "amount": num(a.amount), "source": a.source}
        for a in acts
    ]  # fmt: skip
    return env({**_security(sec), "actions": rows}, acts[-1].ex_date if acts else None, SOURCE)


@server.tool
def resolve_security(query: str, market: str | None = None) -> dict[str, Any]:
    """Resolve an ISIN, symbol or company name to one security id, or return ranked candidates
    with scores when the query is ambiguous or unknown. Optional market filter IN or US."""
    with common.market_ctx() as m:
        master = SecurityMaster(m.sql)
        found = master.lookup(query, market)
        sec = master.get(found.security_id) if found.security_id is not None else None
    data = {
        "security_id": found.security_id, "matched_by": found.matched_by,
        "security": _security(sec) if sec else None,
        "candidates": [
            {"security_id": c.security_id, "symbol": c.symbol, "exchange": c.exchange,
             "name": c.name, "score": round(c.score, 4)}
            for c in found.candidates
        ],
    }  # fmt: skip
    return env(data, None, SOURCE)


@server.tool
def get_last_trading_day(market: str = "NSE") -> dict[str, Any]:
    """The latest session whose close has passed, as an IST date, for NSE or NYSE. The NYSE close
    is approximated as 02:30 IST of the next date all year (an hour early in US summer time)."""
    if market not in MARKETS:
        raise ValueError(f"market must be one of {', '.join(MARKETS)}")
    cfg = common.settings()
    now = common.now()
    day = last_trading_day(calendar_for(MARKETS[market], cfg), now)
    return env({"market": market, "last_trading_day": day.isoformat()}, day, SOURCE)
