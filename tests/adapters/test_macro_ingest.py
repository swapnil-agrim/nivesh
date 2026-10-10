from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import httpx
import pytest

from nivesh_adapters.macro import MacroFetch
from nivesh_adapters.market_ingest import ingest_macro
from nivesh_core.config import MacroSpec, MarketSettings, Settings
from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.errors import NiveshError, SecretNotFound
from nivesh_core.market_store import get_macro
from tests import pii_values as pv

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
D = Decimal


@pytest.fixture
def duck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    monkeypatch.setenv("FRED_API_KEY", pv.fred_key())
    c = duckdb.connect(str(tmp_path / "m.duckdb"))
    migrate.apply(c, MIGRATIONS / "duck")
    yield c
    c.close()


def settings() -> Settings:
    specs = {
        "y10_us": MacroSpec(source="fred", id="DGS10"),
        "usdinr": MacroSpec(source="fred", id="DEXINUS"),
        "fii_net": MacroSpec(source="nse_flows", id="fii"),
        "dii_net": MacroSpec(source="nse_flows", id="dii"),
    }
    return Settings(market=MarketSettings(macro_series=specs))  # type: ignore[arg-type]


class Net:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail: set[str] = set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if any(f in str(req.url) for f in self.fail):
            return httpx.Response(503)
        if "stlouisfed" in req.url.host:
            return httpx.Response(200, text=(FX / "fred_obs.json").read_text())
        return httpx.Response(200, text=(FX / "nse_fiidii.json").read_text())


def fetcher(net: Net) -> object:
    c = httpx.Client(transport=httpx.MockTransport(net))
    return lambda: MacroFetch(c)


def test_ingest_macro_stores_dated_series_idempotently(duck: duckdb.DuckDBPyConnection) -> None:
    net = Net()
    rep = ingest_macro(duck, settings(), fetch=fetcher(net))  # type: ignore[arg-type]
    assert rep.new == {"y10_us": 3, "usdinr": 3, "fii_net": 1, "dii_net": 1} and rep.failed == []
    assert get_macro(duck, "y10_us")[0] == (date(2026, 1, 2), D("4.50000000"))
    assert get_macro(duck, "fii_net") == [(date(2026, 1, 9), D("-1500.50000000"))]
    again = ingest_macro(duck, settings(), refresh=True, fetch=fetcher(net))  # type: ignore[arg-type]
    assert sum(again.new.values()) == 0
    assert duck.execute("select count(*) from macro_series").fetchone() == (8,)


def test_ingest_macro_uses_macro_ttl(duck: duckdb.DuckDBPyConnection) -> None:
    net = Net()
    ingest_macro(duck, settings(), fetch=fetcher(net))  # type: ignore[arg-type]
    n = len(net.requests)
    ingest_macro(duck, settings(), fetch=fetcher(net))  # type: ignore[arg-type]
    assert len(net.requests) == n and n == 3  # one flows call serves both roles


def test_ingest_macro_partial_failure_reports_failed_roles_and_stores_others(
    duck: duckdb.DuckDBPyConnection,
) -> None:
    net = Net()
    net.fail = {"DGS10"}
    rep = ingest_macro(duck, settings(), fetch=fetcher(net))  # type: ignore[arg-type]
    assert any("y10_us" in f for f in rep.failed) and "y10_us" not in rep.new
    assert rep.new["usdinr"] == 3 and get_macro(duck, "y10_us") == []


def test_ingest_macro_role_filter_and_unknown_role(duck: duckdb.DuckDBPyConnection) -> None:
    rep = ingest_macro(duck, settings(), roles=["usdinr"], fetch=fetcher(Net()))  # type: ignore[arg-type]
    assert rep.new == {"usdinr": 3}
    with pytest.raises(NiveshError, match="unknown role 'bogus'.*usdinr"):
        ingest_macro(duck, settings(), roles=["bogus"], fetch=fetcher(Net()))  # type: ignore[arg-type]
    with pytest.raises(NiveshError, match="no macro series configured"):
        ingest_macro(duck, Settings(), fetch=fetcher(Net()))  # type: ignore[arg-type]


def test_ingest_macro_missing_key_is_reported_not_fatal(
    duck: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    monkeypatch.delenv("FRED_API_KEY")
    rep = ingest_macro(duck, settings(), fetch=fetcher(Net()))  # type: ignore[arg-type]
    assert rep.new == {"fii_net": 1, "dii_net": 1}
    assert len(rep.failed) == 2 and all("secrets set FRED_API_KEY" in f for f in rep.failed)
    assert issubclass(SecretNotFound, NiveshError)
