import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from nivesh_adapters.estimates import Estimates
from nivesh_adapters.market_ingest import ingest_estimates
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_store import estimate_history, get_events, latest_estimates
from nivesh_core.security_master import SecurityMaster, SecurityRow, build_master
from tests import pii_values as pv
from tests.market_fx import fmp_calendar, fmp_estimates, mrow

D = Decimal
DAY1, DAY2 = date(2026, 1, 5), date(2026, 1, 6)


@dataclass
class Env:
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    settings: Settings
    master: SecurityMaster

    def sec(self, symbol: str) -> SecurityRow:
        return self.master.by_symbol(symbol)[0]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("FMP_API_KEY", pv.fmp_key())
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("AAPL", "NASDAQ", market="US", currency="USD"),
            mrow("RELIANCE", isin="INE002A01018"),
        ],
        [],
    )
    duck = open_duck(tmp_path / "nivesh.duckdb")
    yield Env(sql, duck, Settings(data_dir=str(tmp_path)), SecurityMaster(sql))
    duck.close()
    sql.close()


class Net:
    def __init__(self) -> None:
        self.n = 0
        self.eps = 7.45

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.n += 1
        if "earning_calendar" in req.url.path:
            return httpx.Response(200, json=fmp_calendar())
        return httpx.Response(200, json=fmp_estimates(eps=self.eps))


def est(net: Net) -> Any:
    c = httpx.Client(transport=httpx.MockTransport(net))
    return lambda: Estimates(c)


def test_ingest_estimates_stores_snapshot_and_next_earnings_event(env: Env) -> None:
    rep = ingest_estimates(env.duck, env.settings, env.sec("AAPL"), today=DAY1,
                           estimates=est(Net()))  # fmt: skip
    assert rep.available and rep.stored == 4 and rep.next_earnings == date(2026, 2, 1)
    latest = {(e.metric, e.period): e.value for e in latest_estimates(env.duck, rep.security_id)}
    assert latest[("revenue", "2026-09-30")] == D("391035000000")
    events = get_events(env.duck, security_id=rep.security_id)
    assert [(e.event_type, e.event_date) for e in events] == [
        ("results", date(2026, 2, 1)),
        ("results", date(2026, 5, 1)),
    ]


def test_daily_snapshot_insert_is_idempotent_per_as_of(env: Env) -> None:
    net = Net()
    sec = env.sec("AAPL")
    ingest_estimates(env.duck, env.settings, sec, today=DAY1, estimates=est(net))
    ingest_estimates(env.duck, env.settings, sec, today=DAY1, refresh=True,
                     estimates=est(net))  # fmt: skip
    assert env.duck.execute("select count(*) from estimate").fetchone() == (4,)
    net.eps = 8.0
    ingest_estimates(env.duck, env.settings, sec, today=DAY2, refresh=True,
                     estimates=est(net))  # fmt: skip
    hist = estimate_history(env.duck, sec.id, "eps", "2026-09-30")
    assert hist == [(DAY1, D("7.45")), (DAY2, D("8.0"))]


def test_estimates_use_estimates_ttl(env: Env) -> None:
    net = Net()
    ingest_estimates(env.duck, env.settings, env.sec("AAPL"), today=DAY1,
                     estimates=est(net))  # fmt: skip
    n = net.n
    ingest_estimates(env.duck, env.settings, env.sec("AAPL"), today=DAY1,
                     estimates=est(net))  # fmt: skip
    assert net.n == n


def test_india_and_missing_key_report_unavailable_and_store_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    rep = ingest_estimates(env.duck, env.settings, env.sec("RELIANCE"), today=DAY1)
    assert rep.available is False and rep.reason and "India" in rep.reason and rep.stored == 0
    monkeypatch.delenv("FMP_API_KEY")
    rep = ingest_estimates(env.duck, env.settings, env.sec("AAPL"), today=DAY1)
    assert rep.available is False and rep.reason and "FMP_API_KEY" in rep.reason
    assert env.duck.execute("select count(*) from estimate").fetchone() == (0,)


def test_failed_source_is_reported_not_fatal(env: Env) -> None:
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    rep = ingest_estimates(env.duck, env.settings, env.sec("AAPL"), today=DAY1,
                           estimates=lambda: Estimates(c))  # fmt: skip
    assert rep.available and rep.stored == 0 and len(rep.failed) == 2
