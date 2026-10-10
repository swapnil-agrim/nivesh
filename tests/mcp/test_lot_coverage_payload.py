import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import nivesh_mcp.holdings as mh
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars
from nivesh_mcp.registry import SERVERS
from tests.us_fx import D, lot, usd_holding

Env = tuple[list[str], Path]
NOW = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


@pytest.fixture
def env(cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch) -> Path:
    args, data = cli_env
    monkeypatch.setenv("NIVESH_CONFIG_DIR", args[1])
    monkeypatch.setattr(mh, "_now", lambda: NOW)
    init_stores(data)
    return data


async def call(tool: str, **args: Any) -> dict[str, Any]:
    async with Client(SERVERS["holdings"].mcp) as c:
        res = await c.call_tool(tool, args)
    out: dict[str, Any] = json.loads(res.content[0].text)  # type: ignore[union-attr]
    return out


def seed(data: Path, lot_qtys: list[str], held: str = "10", digest: str = "d") -> None:
    c = open_sqlite(data / "nivesh.sqlite")
    day = date(2026, 1, 5)
    usd = usd_holding(quantity=D(held), price=D(100), avg_cost=D(100), as_of=day)
    lots = [lot(acquired_on=date(2025, 1, 5), quantity=D(q), cost_per_unit=D(100))
            for q in lot_qtys]  # fmt: skip
    save_ingest(c, kind="us_csv", source_label="y", digest=digest, as_of=day, holdings=[usd],
                txns=[], holder_refs=[], warnings=[], lots=lots)  # fmt: skip
    sid = c.execute("select id from security where symbol = 'AAPL'").fetchone()[0]
    c.close()
    d = open_duck(data / "nivesh.duckdb")
    upsert_bars(d, [PriceBar(security_id=sid, date=day, close=Decimal("110"), source="yahoo")])
    d.close()


async def test_get_lots_keeps_bool_and_adds_coverage_string_for_exact_partial_over(
    env: Path,
) -> None:
    seed(env, ["6", "4"])
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["lots_cover_quantity"] is True and sec["coverage"] == "exact"
    seed(env, ["4"], digest="e")
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["lots_cover_quantity"] is False and sec["coverage"] == "partial"
    seed(env, ["6", "6"], digest="f")
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["lots_cover_quantity"] is False and sec["coverage"] == "over"


async def test_get_lots_over_covered_security_has_null_xirr_and_reason(env: Path) -> None:
    seed(env, ["6", "6"])
    d = (await call("get_lots"))["data"]
    (sec,) = d["securities"]
    assert sec["xirr_usd"] is None and "exceed" in sec["reason_usd"]
    assert d["overall_xirr_usd"] is None and "AAPL" in d["overall_reason_usd"]


async def test_existing_partial_note_text_is_unchanged(env: Path) -> None:
    seed(env, ["4"])
    (sec,) = (await call("get_lots"))["data"]["securities"]
    assert sec["note"] == "XIRR covers only dated lots"
