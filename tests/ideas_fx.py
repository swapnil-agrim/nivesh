"""A synthetic stored universe for the E9 tests: invented tickers only, integer arithmetic, no
holders and no network. Volumes are set so the 20-day value traded is about `crore` crore INR
(India) or `usd_m` million USD (US), whatever the price path does."""

import atexit
import shutil
import sqlite3
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars, write_fundamentals
from nivesh_core.membership import MemberRow, load_members
from nivesh_core.security_master import SecurityMaster, build_master
from tests.analysis_fx import annual_rows, lcg_bars, universe_book
from tests.holdings_fx import holding
from tests.market_fx import mrow

D = Decimal
ASOF = date(2026, 1, 2)  # the committee fixtures' as-of; the last bar falls on it
BARS = 300


def redate(i: int) -> date:
    return ASOF - timedelta(days=BARS - 1 - i)


SECTORS = ("Tech", "Banks", "Energy", "Health")
IN_SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
US_SYMBOLS = ("UAA", "UBB", "UCC", "UDD", "UEE", "UFF")


def isin_of(i: int) -> str:
    return "INE" + f"{i:03d}" + "A01010"


_BUILT: dict[str, Path] = {}


def seed_ideas_store(
    data: Path, *, in_symbols: tuple[str, ...] = IN_SYMBOLS,
    us_symbols: tuple[str, ...] = US_SYMBOLS, thin: tuple[str, ...] = (),
    sectors: dict[str, str | None] | None = None, load: bool = True,
) -> dict[str, int]:  # fmt: skip
    """Stores with `in_symbols` (India, NSE) and `us_symbols` (US, NASDAQ), bars and statements;
    the names in `thin` trade little and fail the floor. With `load`, NIFTY500 holds every Indian
    name, SP500 the first four US names and NASDAQ100 the last four (two overlap). Built once
    per argument set per session, then copied into `data` (the bar upserts dominate the cost)."""
    key = repr((in_symbols, us_symbols, thin, sorted((sectors or {}).items(), key=str), load))
    if key not in _BUILT:
        cache = Path(tempfile.mkdtemp(prefix="nivesh-ideas-"))
        atexit.register(shutil.rmtree, cache, ignore_errors=True)
        _seed(cache / "d", in_symbols, us_symbols, thin, sectors, load)
        _BUILT[key] = cache / "d"
    shutil.copytree(_BUILT[key], data)
    sql = sqlite3.connect(data / "nivesh.sqlite")
    try:
        return {str(r[0]): int(r[1]) for r in sql.execute("SELECT symbol, id FROM security")}
    finally:
        sql.close()


def _seed(
    data: Path, in_symbols: tuple[str, ...], us_symbols: tuple[str, ...], thin: tuple[str, ...],
    sectors: dict[str, str | None] | None, load: bool,
) -> None:  # fmt: skip
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    sector_of = sectors or {}
    try:
        rows = [
            mrow(s, name=f"Example {s}", isin=isin_of(i),
                 sector=sector_of.get(s, SECTORS[i % 4]))
            for i, s in enumerate(in_symbols)
        ]  # fmt: skip
        rows += [
            mrow(s, "NASDAQ", name=f"Example {s}", market="US", currency="USD",
                 sector=sector_of.get(s, SECTORS[i % 4]))
            for i, s in enumerate(us_symbols)
        ]  # fmt: skip
        rows += [
            mrow("BENCHIN", name="Bench India", asset_class="index"),
            mrow("BENCHUS", "NASDAQ", name="Bench US", asset_class="index", market="US",
                 currency="USD"),
        ]  # fmt: skip
        build_master(sql, rows, [])
        ids = {r[0]: int(r[1]) for r in sql.execute("SELECT symbol, id FROM security")}
        duck = open_duck(data / "nivesh.duckdb")
        try:
            for s in ("BENCHIN", "BENCHUS"):
                upsert_bars(
                    duck,
                    [PriceBar(security_id=ids[s], date=redate(i), close=b.close, source="yahoo")
                     for i, b in enumerate(lcg_bars(BARS, seed=999))],
                )  # fmt: skip
            for n, s in enumerate([*in_symbols, *us_symbols], start=1):
                target = D(10_000_000 if s in thin else 200_000_000)  # 1 crore or 20 crore INR
                if s in us_symbols:
                    target = D(5_000_000 if s in thin else 80_000_000)  # 5M or 80M USD
                bars = lcg_bars(BARS, seed=n)
                upsert_bars(
                    duck,
                    [PriceBar(security_id=ids[s], date=redate(i), close=b.close, high=b.high,
                              low=b.low, volume=int(target / b.close), source="yahoo")
                     for i, b in enumerate(bars)],
                )  # fmt: skip
                write_fundamentals(
                    duck, ids[s], annual_rows(universe_book(n), last_fy=2023), "edgar"
                )
        finally:
            duck.close()
        if load:
            master = SecurityMaster(sql)
            as_of = ASOF
            india = [MemberRow(s, isin_of(i), None) for i, s in enumerate(in_symbols)]
            load_members(sql, master, "NIFTY500", "IN", india, as_of)
            us = [MemberRow(s) for s in us_symbols]
            load_members(sql, master, "SP500", "US", us[:4], as_of)
            load_members(sql, master, "NASDAQ100", "US", us[2:], as_of)
    finally:
        sql.close()


BENCH = {"IN": "BENCHIN", "US": "BENCHUS"}


def settings_for(data: Path) -> Settings:
    """Settings whose benchmarks are the fixture's two index securities."""
    return Settings(
        data_dir=str(data), analysis=AnalysisSettings.model_validate({"ta": {"benchmarks": BENCH}})
    )


def with_benchmarks(config_dir: Path) -> None:
    """Point a tmp nivesh.yaml at the fixture's benchmarks."""
    p = config_dir / "nivesh.yaml"
    old = "benchmarks: {}              # market"
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, "benchmarks: {IN: BENCHIN, US: BENCHUS}  # market", 1))


def write_constituents(path: Path, symbols: tuple[str, ...], with_isin: bool = False) -> Path:
    lines = ["symbol,isin,sector"]
    for i, s in enumerate(symbols):
        lines.append(f"{s},{isin_of(i) if with_isin else ''},{SECTORS[i % 4]}")
    path.write_text("\n".join(lines) + "\n")
    return path


def hold(data: Path, *indexes: int, heavy: int | None = None) -> None:
    """Hold the Indian names at these positions of IN_SYMBOLS (5 units each), and `heavy` (a
    position of IN_SYMBOLS) in size, so the small ones sit far under the position limit."""
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        rows = [
            holding(isin=isin_of(i), symbol=IN_SYMBOLS[i], exchange="NSE", name="x",
                    quantity=D(5), price=D(100), avg_cost=D(90), as_of=date(2026, 1, 1))
            for i in indexes
        ]  # fmt: skip
        if heavy is not None:
            rows.append(
                holding(isin=isin_of(heavy), symbol=IN_SYMBOLS[heavy], exchange="NSE", name="x",
                        quantity=D(100000), price=D(100), avg_cost=D(90), as_of=date(2026, 1, 1))
            )  # fmt: skip
        save_ingest(
            sql, kind="investright", source_label="broker", digest=None, as_of=date(2026, 1, 1),
            holdings=rows, txns=[], holder_refs=[""], warnings=[],
        )  # fmt: skip
    finally:
        sql.close()
