"""`nivesh-holdings`: read-only MCP tools over the stored India holdings (ST-2.9).

Payloads use redaction-safe field names (`holder_ref`, `source_label`, ...) and JSON numbers, never
digit-run strings; every output also passes the server-wide redaction wrapper (NFR-3).
"""

import os
import secrets
import string
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from nivesh_adapters.cas import read_statement
from nivesh_adapters.investright import InvestRightClient
from nivesh_adapters.investright_session import TokenStore, http_client, token_path
from nivesh_adapters.quality import parse_decimal
from nivesh_agents.untrusted import wrap_untrusted
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.holder_ref import get_salt
from nivesh_core.holdings import PRECEDENCE, Holding
from nivesh_core.holdings_store import last_ingest_date, latest_holdings, list_transactions
from nivesh_core.secrets import get_secret
from nivesh_core.security_resolver import TableResolver
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.consolidate import consolidate
from nivesh_mcp.base import ReadOnlyServer

server = ReadOnlyServer("holdings")
SOURCE = "nivesh-holdings"
_now = utcnow  # injectable clock (tests)


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


def _row(h: Holding, total: Decimal) -> dict[str, Any]:
    return {
        "holder_ref": h.holder_ref, "source_label": h.source_label, "source": h.source,
        "isin": h.isin, "symbol": h.symbol, "exchange": h.exchange, "name": h.name,
        "asset_class": h.asset_class, "quantity": _num(h.quantity), "avg_cost": _num(h.avg_cost),
        "price": _num(h.price), "price_basis": h.price_basis, "value_inr": _num(h.value_inr),
        "weight": float(h.value_inr / total) if total else 0.0, "as_of": h.as_of.isoformat(),
        "plan": h.plan, "amfi_code": h.amfi_code, "unresolved": h.unresolved,
    }  # fmt: skip


def _rows(holdings: list[Holding]) -> dict[str, Any]:
    total = sum((h.value_inr for h in holdings), Decimal(0))
    return {
        "holdings": [_row(h, total) for h in holdings],
        "totals": {"value_inr": float(total), "count": len(holdings)},
    }


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
    with _db() as conn:
        found = [h for h in latest_holdings(conn) if source in (None, h.source)]
    return _env(_rows(found), _newest(found))


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
    c = consolidate(found, cfg.investright.demat_ref)
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
                "price_basis": r.price_basis, "value_inr": _num(r.value_inr),
                "weight": _num(r.weight), "as_of": r.as_of.isoformat(), "source": r.source,
                "unresolved": r.unresolved,
            }
            for r in c.rows
        ],
        "totals": {
            "value_inr": _num(c.total_value), "invested_inr": _num(c.invested),
            "pnl_inr": _num(c.pnl), "cost_coverage": _num(c.cost_coverage),
        },
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
