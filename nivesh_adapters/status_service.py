"""Health lines for `nivesh status` and the reconciliation summary for `nivesh ingest`
(ST-10.5). Read-only; nothing here prints a session value or a quantity."""

import sqlite3
from datetime import datetime
from pathlib import Path

import duckdb

from nivesh_adapters.investright_session import TokenStore, token_path
from nivesh_core.config import Settings
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.timeutil import ist_date
from nivesh_engine.consolidate import consolidate

RECENT_RUNS = 5


def session_line(data_dir: Path, now: datetime) -> str:
    """The HDFC InvestRight session: valid today, expired, or absent."""
    store = TokenStore(token_path(data_dir), clock=lambda: now)
    issued = store.issued()
    if issued is None:
        return "hdfc session: absent; run `nivesh login`"
    if store.valid():
        return f"hdfc session: valid (issued {issued.isoformat()})"
    return f"hdfc session: expired (issued {issued.isoformat()}); run `nivesh login`"


def ingest_lines(sql: sqlite3.Connection) -> list[str]:
    """Newest ingest per source kind."""
    rows = sql.execute(
        "SELECT kind, MAX(as_of), COUNT(*) FROM ingest GROUP BY kind ORDER BY kind"
    ).fetchall()
    if not rows:
        return ["last ingest: none yet; run `nivesh sync` or `nivesh ingest`"]
    return [f"last ingest {kind}: as of {as_of} ({n} total)" for kind, as_of, n in rows]


def cache_lines(
    duck: duckdb.DuckDBPyConnection | None, settings: Settings, now: datetime
) -> list[str]:
    """Cache entries by adapter against the longest configured TTL: past it, every data type
    would call the entry stale."""
    if duck is None:
        return ["cache: unavailable (the market database is in use by another process)"]
    # ponytail: entries do not record their data type, so the longest TTL is the stale line
    limit = max(getattr(settings.ttls, f) for f in type(settings.ttls).model_fields)
    rows = duck.execute(
        "SELECT adapter, MAX(fetched_at), COUNT(*) FROM cache_entry "
        "GROUP BY adapter ORDER BY adapter"
    ).fetchall()
    if not rows:
        return ["cache: empty"]
    out = []
    for adapter, fetched, n in rows:
        newest = datetime.fromisoformat(fetched)
        flag = f" STALE (older than {limit.days} days)" if now - newest > limit else ""
        out.append(f"cache {adapter}: {n} entries, newest {ist_date(newest).isoformat()}{flag}")
    return out


def run_lines(sql: sqlite3.Connection) -> list[str]:
    rows = sql.execute(
        "SELECT id, command, status, tier, cost_inr FROM run ORDER BY id DESC LIMIT ?",
        (RECENT_RUNS,),
    ).fetchall()
    if not rows:
        return ["last runs: none"]
    return ["last runs:"] + [
        f"  run {i} {cmd} {status} ({tier}) cost {cost:.2f} INR"
        for i, cmd, status, tier, cost in rows
    ]


def health(
    sql: sqlite3.Connection, duck: duckdb.DuckDBPyConnection | None, settings: Settings,
    data_dir: Path, now: datetime,
) -> list[str]:  # fmt: skip
    return [
        session_line(data_dir, now), *ingest_lines(sql), *cache_lines(duck, settings, now),
        *run_lines(sql),
    ]  # fmt: skip


def ingest_summary(sql: sqlite3.Connection, scope_ref: str | None) -> list[str]:
    """Where the consolidated book comes from and where InvestRight and the depository CAS
    disagree: counts and ISINs only, never quantities."""
    held = latest_holdings(sql)
    if not held:
        return ["reconciliation: no holdings stored"]
    book = consolidate(held, scope_ref)
    parts = ", ".join(f"{c.source} {c.holdings} ({c.share * 100:.0f}%)" for c in book.coverage)
    out = [f"holdings by source: {parts}"]
    if book.reconciliation:
        isins = ", ".join(r.isin for r in book.reconciliation[:5])
        more = len(book.reconciliation) - 5
        out.append(
            f"reconciliation: {len(book.reconciliation)} ISIN(s) differ between InvestRight and "
            f"CAS ({isins}{f', +{more} more' if more > 0 else ''})"
        )
    else:
        out.append("reconciliation: no differences (or only one source stored)")
    return out
