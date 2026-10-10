"""Helpers shared by the E4 market MCP servers: paging, caps, safe numbers, envelopes and a
read-only store context.

Payload rules (redaction-safe): numbers are JSON numbers, never digit-run strings; field names
avoid `key`, `client`, `account`, `token`, `address`; external text goes through `wrap`.
"""

import os
import secrets
import sqlite3
import string
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import duckdb

from nivesh_agents.untrusted import wrap_untrusted
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.security_master import SecurityMaster
from nivesh_core.timeutil import utcnow

T = TypeVar("T")
MAX_ROWS = 500
MAX_TEXT_CHARS = 20000
BUSY = "market data is being updated (ingest in progress); retry shortly"
now = utcnow  # injectable clock (tests patch `nivesh_mcp.common.now`)


def page(rows: list[T], limit: int, cursor: int = 0) -> tuple[list[T], int | None]:
    """One page of `rows`; `cursor` is a non-negative offset, `limit` clamps to [1, MAX_ROWS]."""
    if cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    size = max(1, min(limit, MAX_ROWS))
    chunk = rows[cursor : cursor + size]
    nxt = cursor + size
    return chunk, (nxt if nxt < len(rows) else None)


def text_chunk(text: str, offset: int = 0, limit: int = MAX_TEXT_CHARS) -> tuple[str, int | None]:
    """A slice of long text (at most MAX_TEXT_CHARS) and the next offset, if any."""
    if offset < 0:
        raise ValueError("offset must be a non-negative integer")
    size = max(1, min(limit, MAX_TEXT_CHARS))
    nxt = offset + size
    return text[offset:nxt], (nxt if nxt < len(text) else None)


def parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO date YYYY-MM-DD, got {value!r}") from None


def letters_nonce() -> str:
    """Letters only, so the digit-run redaction can never mangle the untrusted-data tag."""
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(16))


def wrap(text: str, source: str) -> str:
    return wrap_untrusted(text, source, nonce=letters_nonce)


MAX_URL = 500
MAX_SOURCE = 100


def safe_url(url: str | None) -> str | None:
    """A feed-supplied URL only if it is http(s) and at most 500 characters, else None."""
    if not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    return url if url.lower().startswith(("http://", "https://")) else None


def safe_source(source: str) -> str:
    return source if len(source) <= MAX_SOURCE else source[:MAX_SOURCE] + "..."


def num(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def env(
    data: Any,
    as_of: date | datetime | str | None,
    source: str,
    *,
    stale: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    stamp = as_of.isoformat() if isinstance(as_of, date | datetime) else as_of or now().isoformat()
    return {"data": data, "as_of": stamp, "source": source, "stale": stale, **extra}


@dataclass
class MarketCtx:
    settings: Settings
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection


_ready: set[Path] = set()  # data dirs whose stores this process has already upgraded


def settings() -> Settings:
    return load_settings(Path(os.environ.get("NIVESH_CONFIG_DIR", "config")) / "nivesh.yaml")


@contextmanager
def market_ctx() -> Iterator[MarketCtx]:
    """SQLite plus a read-only DuckDB. `init_stores` runs once per process per data dir."""
    cfg = settings()
    data_dir = Path(cfg.data_dir).resolve()
    try:
        if data_dir not in _ready:
            init_stores(data_dir)
            _ready.add(data_dir)
        duck = open_duck(data_dir / "nivesh.duckdb", read_only=True)
    except (duckdb.ConnectionException, duckdb.IOException):
        raise NiveshError(BUSY) from None
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        yield MarketCtx(cfg, sql, duck)
    except (duckdb.ConnectionException, duckdb.IOException):
        raise NiveshError(BUSY) from None
    finally:
        sql.close()
        duck.close()


def resolve_one(master: SecurityMaster, query: str, market: str | None = None) -> int:
    """One security id for any ISIN/symbol/name, else a tool error listing up to 5 candidates."""
    found = master.lookup(query, market)
    if found.security_id is not None:
        return found.security_id
    if found.candidates:
        names = ", ".join(f"{c.symbol} ({c.exchange})" for c in found.candidates[:5])
        raise ValueError(f"{query!r} is ambiguous; candidates: {names}")
    raise ValueError(f"no security matches {query!r}")
