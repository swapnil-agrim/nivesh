"""Tiny stores for ledger-scoring tests: master rows, daily bars on a straight line, ledger rows.
Calendar days only; the line makes every return easy to compute by hand."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.ledger import LedgerEntry, record_call
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars
from nivesh_core.security_master import build_master
from tests.market_fx import mrow

D = Decimal
ISSUE = date(2026, 1, 5)  # a Monday
NOW = datetime(2026, 1, 5, 9, tzinfo=UTC)


@dataclass
class Fx:
    data: Path
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    settings: Settings
    ids: dict[str, int]

    def close(self) -> None:
        self.sql.close()
        self.duck.close()


def settings_with(data: Path, **blocks: Any) -> Settings:
    cfg: dict[str, Any] = {
        "data_dir": str(data),
        "analysis": {"ta": {"benchmarks": {"IN": "BENCHIN", "US": "BENCHUS"}}},
    }
    cfg.update(blocks)
    return Settings.model_validate(cfg)


def make_fx(tmp_path: Path, **blocks: Any) -> Fx:
    data = tmp_path / "data"
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    rows = [
        mrow("AAA", name="Alpha One"),
        mrow("BBB", name="Beta Two"),
        mrow("UUU", "NASDAQ", name="Upsilon", market="US", currency="USD"),
        mrow("BENCHIN", name="Bench India", asset_class="index"),
        mrow("BENCHUS", "NASDAQ", name="Bench US", asset_class="index", market="US",
             currency="USD"),
    ]  # fmt: skip
    build_master(sql, rows, [])
    ids = {str(r[0]): int(r[1]) for r in sql.execute("SELECT symbol, id FROM security")}
    sql.execute("INSERT INTO run (id, command, started_at, status) VALUES (1, 'ideas', 't', 'ok')")
    duck = open_duck(data / "nivesh.duckdb")
    return Fx(data, sql, duck, settings_with(data, **blocks), ids)


def line_bars(
    fx: Fx, symbol: str, first: date, last: date, start: int = 100, step: int = 1,
    source: str = "yahoo",
) -> None:  # fmt: skip
    """One bar per calendar day from `first` to `last`; close = start + step * days since issue."""
    out, d = [], first
    while d <= last:
        close = D(start + step * (d - ISSUE).days)
        out.append(PriceBar(security_id=fx.ids[symbol], date=d, close=close, source=source))
        d += timedelta(days=1)
    upsert_bars(fx.duck, out)


def add_entry(fx: Fx, symbol: str, verdict: str = "BUY", **over: Any) -> int:
    base: dict[str, Any] = dict(
        run_id=1, security_id=fx.ids[symbol], verdict=verdict, horizon="positional_1_6m",
        conviction="high", review_date=date(2026, 7, 1), benchmark_reason="none",
        input_hash="h", prompt_versions={}, model_versions={}, reported=True,
        as_of=ISSUE, price_date=ISSUE,
    )  # fmt: skip
    base.update(over)
    return record_call(fx.sql, LedgerEntry(**base), NOW)
