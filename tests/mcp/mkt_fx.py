"""Shared helpers for the E4 MCP server tests: a seeded master and a tool-call shortcut."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from fastmcp import Client

from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.security_master import build_master
from nivesh_mcp.registry import SERVERS
from tests import pii_values as pv
from tests.market_fx import mrow

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)  # Monday 17:30 IST, after the NSE close


@dataclass
class Mkt:
    data: Path
    ids: dict[str, int]

    @contextmanager
    def duck(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Read-write handle for seeding; always closed before a tool opens the file read-only."""
        c = open_duck(self.data / "nivesh.duckdb")
        try:
            yield c
        finally:
            c.close()


def seed_master(data: Path) -> dict[str, int]:
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("RELIANCE", name="Reliance Industries Limited", isin="INE002A01018",
                 bse_code="500325", industry="Refineries", sector="Energy"),
            mrow("ONGC", name="Oil and Natural Gas Corporation Limited", isin="INE213A01029",
                 industry="Refineries", sector="Energy"),
            mrow("TCS", name="Tata Consultancy Services Limited", isin="INE467B01029",
                 industry="IT - Software"),
            mrow("TATAMOTORS", name="Tata Motors Limited", isin="INE155A01022"),
            mrow("HDFCBANK", name="HDFC Bank Limited", isin="INE040A01034", industry="Banks"),
            mrow("AAPL", "NASDAQ", name="Apple Inc.", market="US", currency="USD",
                 cik=pv.cik_synthetic(), industry="Consumer Electronics"),
            mrow("NIFTY 50", name="NIFTY 50", asset_class="index"),
            mrow("NIFTY BANK", name="NIFTY BANK", asset_class="index"),
        ],
        [],
    )  # fmt: skip
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    sql.close()
    return ids


async def call(server: str, tool: str, **args: Any) -> dict[str, Any]:
    async with Client(SERVERS[server].mcp) as c:
        res = await c.call_tool(tool, args)
    out: dict[str, Any] = json.loads(res.content[0].text)  # type: ignore[union-attr]
    return out
