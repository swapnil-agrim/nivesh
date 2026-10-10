"""`nivesh-holdings`: read-only MCP tools over the stored India holdings (ST-2.9).

Payloads use redaction-safe field names (`holder_ref`, `source_label`, ...) and JSON numbers, never
digit-run strings; every output also passes the server-wide redaction wrapper (NFR-3).
"""

import os
import secrets
import string
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from nivesh_adapters.cas import read_statement
from nivesh_adapters.investright import InvestRightClient
from nivesh_adapters.investright_session import TokenStore, http_client, token_path
from nivesh_adapters.quality import parse_decimal
from nivesh_agents.untrusted import wrap_untrusted
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.holder_ref import get_salt
from nivesh_core.holdings import PRECEDENCE, Holding
from nivesh_core.holdings_store import (
    last_ingest_date,
    latest_holdings,
    latest_lots,
    list_transactions,
)
from nivesh_core.market_store import get_bars, get_macro
from nivesh_core.secrets import get_secret
from nivesh_core.security_resolver import TableResolver
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.consolidate import Exposure, consolidate
from nivesh_engine.fx import (
    VALUATION_MAX_AGE_DAYS,
    FxResult,
    convert_holdings,
    rate_on_or_before,
    unavailable,
)
from nivesh_engine.returns import (
    MAX_LOT_RATE_AGE_DAYS,
    NO_PRICE,
    Key,
    LotReport,
    lot_report,
)
from nivesh_mcp.base import ReadOnlyServer
from nivesh_mcp.common import page

server = ReadOnlyServer("holdings")
SOURCE = "nivesh-holdings"
_now = utcnow  # injectable clock (tests)
ZERO = Decimal(0)


def _nonce() -> str:
    """Letters only, so the digit-run redaction can never mangle the untrusted-data tag."""
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(16))


def _settings() -> Settings:
    return load_settings(Path(os.environ.get("NIVESH_CONFIG_DIR", "config")) / "nivesh.yaml")


@contextmanager
def _db() -> Iterator[Any]:
    data_dir = Path(_settings().data_dir)
    init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        yield conn
    finally:
        conn.close()


def _store() -> TokenStore:
    return TokenStore(token_path(Path(_settings().data_dir)), lambda: _now())


def _num(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def _env(
    data: Any, as_of: date | str | None = None, source: str = SOURCE, **extra: Any
) -> dict[str, Any]:
    stamp = as_of.isoformat() if isinstance(as_of, date) else as_of or _now().isoformat()
    return {"data": data, "as_of": stamp, "source": source, "stale": False, **extra}


def _fx(on: date, cfg: Settings) -> FxResult:
    """USDINR for `on` from the stored macro series; unavailable (never zero) on any gap."""
    spec = cfg.market.macro_series.get("usdinr")
    if spec is None:
        return unavailable("usdinr is not configured under market.macro_series")
    try:
        duck = open_duck(Path(cfg.data_dir) / "nivesh.duckdb", read_only=True)
        try:
            obs = get_macro(duck, "usdinr", on - timedelta(days=VALUATION_MAX_AGE_DAYS + 10), on)
        finally:
            duck.close()
    except (duckdb.ConnectionException, duckdb.IOException):
        return unavailable("market data busy (ingest in progress); USDINR rate not read")
    return rate_on_or_before(
        obs, on, f"{spec.source}:{spec.id}", max_age_days=VALUATION_MAX_AGE_DAYS
    )


def _fx_block(fx: FxResult | None) -> dict[str, Any] | None:
    if fx is None:
        return None
    return {
        "available": fx.available, "fx_rate": _num(fx.rate),
        "fx_date": fx.rate_date.isoformat() if fx.rate_date else None, "fx_source": fx.source,
        "stale": fx.stale, "reason": fx.reason,
    }  # fmt: skip


def _exposure_rows(items: list[Exposure]) -> list[dict[str, Any]]:
    return [
        {
            "currency": e.currency, "exposure_pct": _num(e.pct), "value_inr": _num(e.value_inr),
            "value_usd": _num(e.value_native), "available": e.available,
        }
        for e in items
    ]  # fmt: skip


def _row(h: Holding, total: Decimal) -> dict[str, Any]:
    return {
        "holder_ref": h.holder_ref, "source_label": h.source_label, "source": h.source,
        "isin": h.isin, "symbol": h.symbol, "exchange": h.exchange, "name": h.name,
        "asset_class": h.asset_class, "quantity": _num(h.quantity), "avg_cost": _num(h.avg_cost),
        "price": _num(h.price), "price_basis": h.price_basis, "currency": h.currency,
        "value_usd": _num(h.value_native) if h.currency != "INR" else None,
        "value_inr": _num(h.value_inr),
        "weight": None if h.value_inr is None else (float(h.value_inr / total) if total else 0.0),
        "as_of": h.as_of.isoformat(),
        "plan": h.plan, "amfi_code": h.amfi_code, "unresolved": h.unresolved,
    }  # fmt: skip


def _rows(holdings: list[Holding]) -> dict[str, Any]:
    total = sum((h.value_inr for h in holdings if h.value_inr is not None), Decimal(0))
    foreign = [h for h in holdings if h.currency != "INR"]
    return {
        "holdings": [_row(h, total) for h in holdings],
        "totals": {
            "value_inr": float(total), "count": len(holdings),
            **(
                {
                    "value_usd": float(sum((h.value_native for h in foreign), Decimal(0))),
                    "unavailable_count": sum(1 for h in holdings if h.value_inr is None),
                }
                if foreign
                else {}
            ),
        },
    }  # fmt: skip


def _newest(holdings: list[Holding]) -> date | None:
    return max((h.as_of for h in holdings), default=None)


@server.tool
def session_status() -> dict[str, Any]:
    """Whether today's InvestRight login is still valid (IST date); never returns the token."""
    store = _store()
    issued = store.issued()
    return _env(
        {
            "session_valid": store.valid(),
            "issued_ist_date": issued.isoformat() if issued else None,
            "ist_today": ist_date(_now()).isoformat(),
        }
    )


@server.tool
def get_holdings(source: str | None = None) -> dict[str, Any]:
    """Stored holdings with weights and totals; optional filter by source name."""
    if source is not None and source not in PRECEDENCE:
        raise ValueError(f"unknown source {source!r}; use one of {', '.join(PRECEDENCE)}")
    cfg = _settings()
    with _db() as conn:
        found = [h for h in latest_holdings(conn) if source in (None, h.source)]
    fx = _fx(ist_date(_now()), cfg) if any(h.currency != "INR" for h in found) else None
    if fx is not None:
        found = convert_holdings(found, fx)
    data = {
        **_rows(found), "fx": _fx_block(fx),
        "exposure": _exposure_rows(consolidate(found).exposure) if fx is not None else [],
    }  # fmt: skip
    return _env(data, _newest(found))


@server.tool
def get_transactions(isin: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Stored fund transactions (newest first), optionally for one ISIN."""
    with _db() as conn:
        txns = list_transactions(conn, isin=isin, limit=max(1, min(limit, 1000)))
    rows = [
        {
            "holder_ref": t.holder_ref, "isin": t.isin, "symbol": t.symbol, "name": t.name,
            "txn_date": t.txn_date.isoformat(), "txn_type": t.txn_type,
            "quantity": _num(t.quantity), "price": _num(t.price), "amount": _num(t.amount),
        }
        for t in txns
    ]  # fmt: skip
    return _env(
        {"transactions": rows, "count": len(rows)}, max((t.txn_date for t in txns), default=None)
    )


@server.tool
def combined_portfolio() -> dict[str, Any]:
    """One consolidated portfolio: weights, value, invested, P&L, coverage and reconciliation."""
    cfg = _settings()
    with _db() as conn:
        found = latest_holdings(conn)
        last_sync = last_ingest_date(conn, "investright")
    fx = _fx(ist_date(_now()), cfg) if any(h.currency != "INR" for h in found) else None
    c = consolidate(found, cfg.investright.demat_ref, fx)
    notes = list(c.notes)
    if not _store().valid():
        when = (
            f"the last InvestRight sync was {last_sync}"
            if last_sync
            else "no InvestRight sync is stored"
        )
        notes.append(f"No InvestRight session today (run `nivesh login`); {when}")
    data = {
        "holdings": [
            {
                "isin": r.isin, "symbol": r.symbol, "exchange": r.exchange, "name": r.name,
                "asset_class": r.asset_class, "quantity": _num(r.quantity),
                "avg_cost": _num(r.avg_cost), "price": _num(r.price),
                "price_basis": r.price_basis, "currency": r.currency,
                "value_usd": _num(r.value_native), "value_inr": _num(r.value_inr),
                "weight": _num(r.weight), "as_of": r.as_of.isoformat(), "source": r.source,
                "unresolved": r.unresolved,
            }
            for r in c.rows
        ],
        "totals": {
            "value_inr": _num(c.total_value), "invested_inr": _num(c.invested),
            "pnl_inr": _num(c.pnl), "cost_coverage": _num(c.cost_coverage),
            **(
                {
                    "value_usd": _num(sum((r.value_native or ZERO for r in c.rows), ZERO)),
                    "unavailable_count": sum(1 for r in c.rows if r.value_inr is None),
                }
                if c.fx is not None
                else {}
            ),
        },
        "fx": _fx_block(c.fx),
        "exposure": _exposure_rows(c.exposure),
        "coverage": [
            {"source": s.source, "holdings": s.holdings, "value_inr": _num(s.value_inr),
             "share": _num(s.share)}
            for s in c.coverage
        ],
        "reconciliation": [
            {
                "isin": i.isin, "investright_quantity": _num(i.investright_quantity),
                "cas_quantity": _num(i.cas_quantity),
                "investright_as_of": i.investright_as_of.isoformat(),
                "cas_as_of": i.cas_as_of.isoformat(),
            }
            for i in c.reconciliation
        ],
        "notes": notes,
    }  # fmt: skip
    return _env(data, c.as_of)


def _live_client() -> InvestRightClient:
    cfg = _settings().investright
    bearer_value = _store().token()  # SessionExpired("... run `nivesh login`") when not valid today
    return InvestRightClient(
        cfg.base_url, get_secret(cfg.app_key.removeprefix("ref:")), bearer_value, cfg.user_agent,
        http_client(),
    )  # fmt: skip


def _numbers(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        try:
            out[k] = float(parse_decimal("investright", k, v)) if isinstance(v, str) else v
        except NiveshError:
            out[k] = v
    return out


@server.tool
def get_positions() -> dict[str, Any]:
    """Live InvestRight net positions (needs today's login)."""
    res = _live_client().positions()
    rows = [
        {
            "symbol": r.get("security_id"), "isin": r.get("isin"), "exchange": r.get("exchange"),
            "quantity": _numbers(r).get("quantity"), "avg_cost": _numbers(r).get("average_price"),
            "price": _numbers(r).get("last_price"),
        }
        for r in res.data
    ]  # fmt: skip
    return _env({"positions": rows, "count": len(rows)}, res.as_of.isoformat(), res.source)


@server.tool
def get_funds() -> dict[str, Any]:
    """Live InvestRight funds and margins (needs today's login)."""
    res = _live_client().margins()
    return _env(_numbers(res.data), res.as_of.isoformat(), res.source)


def _inbox_file(name: str) -> Path:
    if not name or name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        raise ValueError("file_name must be a bare file name inside the inbox")
    inbox = Path(_settings().data_dir) / "inbox"
    path = inbox / name
    if path.suffix.lower() != ".pdf" or path.is_symlink() or not path.is_file():
        raise ValueError("file_name must be an existing regular .pdf file in the inbox")
    if path.resolve().parent != inbox.resolve():
        raise ValueError("file_name must be a bare file name inside the inbox")
    return path


@server.tool
def read_cas_statement(file_name: str) -> dict[str, Any]:
    """Parse one CAS PDF from the inbox into a PII-free summary; nothing is stored."""
    path = _inbox_file(file_name)
    with _db() as conn:
        parsed = read_statement(path, get_secret("CAS_PASSWORD"), get_salt(), TableResolver(conn))
    warnings = wrap_untrusted("\n".join(parsed.warnings), "cas-parse-warnings", nonce=_nonce)
    data = {
        "kind": parsed.kind,
        "statement_date": parsed.as_of.isoformat(),
        "transactions_count": len(parsed.txns),
        "holder_refs": parsed.holder_refs,
        "warnings": warnings if parsed.warnings else None,
        **_rows(parsed.holdings),
    }
    return _env(data, parsed.as_of)


def _fold(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-")


PriceMap = dict[Key, tuple[Decimal, date] | None]
BUSY_NOTE = "market data busy (ingest in progress); stored prices and rates not read"


def _market_reads(
    cfg: Settings, ids: dict[Key, int], valuation: date, since: date
) -> tuple[PriceMap, list[tuple[date, Decimal]] | None, str | None]:
    """Latest stored US close per security and the USDINR series, from one read-only DuckDB
    open. (prices, None, reason) when the store is busy; obs None when usdinr is not configured."""
    out: PriceMap = dict.fromkeys(ids)
    spec = cfg.market.macro_series.get("usdinr")
    obs: list[tuple[date, Decimal]] | None = None
    try:
        duck = open_duck(Path(cfg.data_dir) / "nivesh.duckdb", read_only=True)
        try:
            for key, sid in ids.items():
                bars = get_bars(duck, sid, end=valuation)
                out[key] = (bars[-1].close, bars[-1].date) if bars else None
            if spec is not None:
                obs = get_macro(duck, "usdinr", since, valuation)
        finally:
            duck.close()
    except (duckdb.ConnectionException, duckdb.IOException):
        return out, None, BUSY_NOTE
    return out, obs, None


def _security_ids(conn: Any, keys: list[Key]) -> dict[Key, int]:
    out: dict[Key, int] = {}
    for sym, exch in keys:
        row = conn.execute(
            "SELECT id FROM security WHERE symbol = ? AND exchange = ?", (sym, exch)
        ).fetchone()
        if row:
            out[(sym, exch)] = int(row[0])
    return out


def _lot_rows(rep: LotReport) -> list[dict[str, Any]]:
    return [
        {
            "symbol": s.symbol, "exchange": s.exchange, "currency": "USD",
            "acquired_on": ln.acquired_on.isoformat(), "quantity": _num(ln.quantity),
            "cost_per_unit": _num(ln.cost_per_unit), "holding_days": ln.holding_days,
            "long_term": ln.long_term,
        }
        for s in rep.securities
        for ln in s.lots
    ]  # fmt: skip


def _inr_reason(reason: str | None, busy: str | None, configured: bool) -> str | None:
    if busy and reason in (NO_PRICE, "no USDINR series supplied"):
        return busy
    if not configured and reason == "no USDINR series supplied":
        return "usdinr is not configured under market.macro_series"
    return reason


def _securities(rep: LotReport, busy: str | None, configured: bool) -> list[dict[str, Any]]:
    return [
        {
            "symbol": s.symbol, "exchange": s.exchange,
            "holding_quantity": _num(s.holding_quantity),
            "lots_cover_quantity": s.lots_cover_quantity, "price": _num(s.price),
            "price_date": s.price_date.isoformat() if s.price_date else None,
            "xirr_usd": _num(s.xirr_usd),
            "reason_usd": busy if busy and s.reason_usd == NO_PRICE else s.reason_usd,
            "xirr_inr": _num(s.xirr_inr),
            "reason_inr": _inr_reason(s.reason_inr, busy, configured),
            "note": s.note,
        }
        for s in rep.securities
    ]  # fmt: skip


@server.tool
def get_lots(symbol: str | None = None, limit: int = 200, cursor: int = 0) -> dict[str, Any]:
    """Dated US lots per security: acquisition date, holding days, XIRR in USD and INR terms, and
    a long-term flag only when tax.us_long_term_days is set (informational, from config). An
    unavailable value carries a reason and is never zero. Optional symbol filter; paged."""
    cfg = _settings()
    valuation = ist_date(_now())
    with _db() as conn:
        lots = latest_lots(conn)
        held: dict[Key, Decimal] = {}
        for h in latest_holdings(conn):
            if h.source == "us_csv":
                held[(h.symbol, h.exchange)] = (
                    held.get((h.symbol, h.exchange), Decimal(0)) + h.quantity
                )
        if symbol is not None:
            want = _fold(symbol)
            lots = [x for x in lots if _fold(x.symbol) == want]
            held = {k: v for k, v in held.items() if _fold(k[0]) == want}
            if not lots and not held:
                raise ValueError(f"no US lots for {symbol!r}")
        ids = _security_ids(conn, sorted({*held, *((x.symbol, x.exchange) for x in lots)}))
    first = min((x.acquired_on for x in lots), default=valuation)
    since = first - timedelta(days=MAX_LOT_RATE_AGE_DAYS + 10)
    prices, obs, busy = _market_reads(cfg, ids, valuation, since)
    threshold = cfg.tax.us_long_term_days
    spec = cfg.market.macro_series.get("usdinr")
    rep = lot_report(
        lots, held, prices, valuation, long_term_days=threshold, usdinr=obs,
        usdinr_source=f"{spec.source}:{spec.id}" if spec else "usdinr",
    )  # fmt: skip
    flat = _lot_rows(rep)
    rows, nxt = page(flat, limit, cursor)
    data = {
        "valuation_date": valuation.isoformat(), "lots": rows, "total": len(flat),
        "securities": _securities(rep, busy, spec is not None),
        "overall_xirr_usd": _num(rep.overall_xirr_usd),
        "overall_reason_usd": rep.overall_reason_usd,
        "overall_xirr_inr": _num(rep.overall_xirr_inr),
        "overall_reason_inr": rep.overall_reason_inr,
        "long_term_days": threshold,
        "long_term_basis": "parameter from config, informational" if threshold else None,
    }  # fmt: skip
    return _env(data, valuation, next_cursor=nxt)
