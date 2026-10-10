"""The screener at the scale of the acceptance criterion: 1,000 securities in under 60 seconds.

Marked `bench`, so it is outside `make check`; run it with
`uv run pytest tests/bench -m bench -q -s` and read the printed figure.
"""

import re
import time
from datetime import date
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Any

import pytest
import yaml

from nivesh_adapters.analysis_data import load_screen_inputs
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_store import write_estimates, write_fundamentals
from nivesh_core.security_master import build_master
from nivesh_engine.metrics import inputs_needed
from nivesh_engine.screen import parse_rules, screen
from tests.analysis_fx import annual_rows, day, universe_book
from tests.market_fx import mrow

D = Decimal
N, BARS = 1000, 500
ASOF = day(BARS - 1)
TABLES = ("price_bar", "corp_action", "fundamental", "shareholding", "estimate", "filing")


class Recording:
    """A connection proxy that keeps the text of every statement."""

    def __init__(self, conn: Any) -> None:
        self.conn, self.sql = conn, []

    def execute(self, text: str, *args: Any, **kw: Any) -> Any:
        self.sql.append(text)
        return self.conn.execute(text, *args, **kw)


def ids_of(ids: dict[str, int], names: list[str]) -> list[int]:
    return [ids[n] for n in names]


def seed(tmp_path: Path) -> tuple[Any, Any, list[int]]:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    names = [f"B{i:04d}" for i in range(N)]
    build_master(
        sql,
        [
            mrow("SPX", "NASDAQ", name="SP 500", market="US", currency="USD", asset_class="index"),
            *(mrow(n, "NASDAQ", name=n, market="US", currency="USD", sector="Tech") for n in names),
        ],
        [],
    )
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    duck = open_duck(tmp_path / "nivesh.duckdb")
    # one INSERT ... SELECT per run: row-by-row inserts of 500,000 bars would dwarf the screen
    duck.execute(
        "INSERT INTO price_bar SELECT s.id, DATE '2024-01-01' + CAST(d.i AS INTEGER), "
        "c.px - 0.4, c.px + 0.6, c.px - 0.6, c.px, 1000 + (d.i * 37 + s.id) % 500, NULL, 'yahoo', "
        "NULL, NULL FROM (SELECT unnest(?) AS id) s CROSS JOIN range(?) d(i), "
        "LATERAL (SELECT CAST(100 + d.i / 5.0 + ((d.i * 17 + s.id * 31) % 23) "
        "- (s.id % 7) * d.i / 150.0 AS DECIMAL(18,6)) AS px) c",
        ([ids["SPX"], *ids_of(ids, names)], BARS),
    )
    for k, n in enumerate(names, start=1):
        write_fundamentals(duck, ids[n], annual_rows(universe_book(k), last_fy=2023), "edgar")
        write_estimates(duck, ids[n], [("eps", "2025-12-31", D("5.0"))], day(BARS - 60), "fmp")
        write_estimates(duck, ids[n], [("eps", "2025-12-31", D("5.5"))], ASOF, "fmp")
    return duck, sql, [ids[n] for n in names]


@pytest.mark.bench
def test_screen_1000_securities_under_60_seconds(tmp_path: Path) -> None:
    duck, sql, ids = seed(tmp_path)
    cfg = AnalysisSettings.model_validate(
        {"ta": {"benchmarks": {"US": "SPX"}}, "valuation": {"min_obs": 6, "history_years": [1]}}
    )
    metrics = ["roce", "net_debt_ebitda", "price_vs_sma200_pct", "valuation_percentile"]
    metrics += ["revisions", "setup_type", "rs_percentile"]
    ops = {"setup_type": ("!=", "downtrend")}
    rules = parse_rules(
        yaml.safe_dump(
            {
                "rules": [
                    {"id": m, "metric": m, "op": ops.get(m, (">=", 0))[0],
                     "value": ops.get(m, (">=", -1000))[1]}
                    for m in metrics
                ]
            }
        )
    )  # fmt: skip
    spy = Recording(duck)
    start = time.perf_counter()
    inputs = load_screen_inputs(spy, sql, ids, ASOF, cfg, needs=inputs_needed(metrics))
    loaded = time.perf_counter()
    res = screen(inputs, rules, as_of=ASOF, cfg=cfg)
    done = time.perf_counter()
    print(f"\nscreen of {N} securities: load {loaded - start:.1f}s, rules {done - loaded:.1f}s")
    assert done - start < 60
    assert res.evaluated == N and res.matches and res.skipped == ()
    chunks = ceil(N / 500)
    for table in TABLES:  # a constant number of statements per table, never one per security
        hits = sum(1 for t in spy.sql if re.search(rf"\bFROM {table}\b", t))
        assert hits <= 4 * chunks, (table, hits)
    assert len(spy.sql) <= 12 * chunks
    duck.close()
    sql.close()


_ = date
