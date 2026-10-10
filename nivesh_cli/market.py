"""`nivesh master build` and `nivesh market ...` (E4). Registered by main.py."""

import functools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import duckdb
import httpx
import typer

from nivesh_adapters.cache import cached_fetch
from nivesh_adapters.edgar import Edgar
from nivesh_adapters.estimates import Estimates
from nivesh_adapters.fundamentals_in import IndiaFundamentals
from nivesh_adapters.macro import MacroFetch
from nivesh_adapters.market_ingest import (
    ingest_estimates,
    ingest_filings,
    ingest_fundamentals,
    ingest_macro,
    ingest_news,
    ingest_prices,
)
from nivesh_adapters.master_sources import (
    PARSERS,
    MasterSources,
    default_indices,
)
from nivesh_adapters.news import Feeds
from nivesh_adapters.prices_in import IndiaPrices
from nivesh_adapters.prices_us import UsPrices
from nivesh_cli.common import settings_of, user_errors
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.market_store import get_news
from nivesh_core.security_master import (
    MasterRow,
    SecurityMaster,
    SecurityRow,
    build_master,
    load_renames,
)

master_app = typer.Typer(no_args_is_help=True, help="Security master.")
market_app = typer.Typer(no_args_is_help=True, help="Market data ingest (read-only sources).")

LOCAL_FILES = {
    "nse": "nse_equity_l.csv",
    "bse": "bse_scrips.csv",
    "amfi": "amfi_navall.txt",
    "sec": "sec_tickers_exchange.json",
}


@dataclass
class Stores:
    settings: Settings
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection


@contextmanager
def stores(ctx: typer.Context) -> Iterator[Stores]:
    """Both stores, DuckDB read-write (ingest writes the cache and market tables)."""
    settings = settings_of(ctx)
    data_dir = Path(settings.data_dir)
    init_stores(data_dir)
    try:
        duck = open_duck(data_dir / "nivesh.duckdb")
    except (duckdb.ConnectionException, duckdb.IOException):
        raise NiveshError(
            "the market database is in use by another process; retry shortly"
        ) from None
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        yield Stores(settings, sql, duck)
    finally:
        sql.close()
        duck.close()


def _master_sources() -> MasterSources:  # seam: tests inject a MockTransport client
    return MasterSources()


def _rows_for(
    source: str, from_dir: Path | None, st: Stores, adapter: MasterSources, refresh: bool
) -> list[MasterRow]:
    if from_dir is not None:
        return PARSERS[source]((from_dir / LOCAL_FILES[source]).read_text())
    res = cached_fetch(
        adapter, {"resource": source}, "master", conn=st.duck, ttls=st.settings.ttls,
        refresh=refresh, redact=False,
    )  # fmt: skip
    return PARSERS[source](res.data)


@master_app.command("build")
def master_build(
    ctx: typer.Context,
    from_dir: Annotated[
        Path | None, typer.Option(help="Read local source files instead of the network.")
    ] = None,
    refresh: Annotated[bool, typer.Option(help="Bypass the master cache.")] = False,
) -> None:
    """Build or refresh the security master (ISIN, symbols, AMFI codes, US tickers, indices)."""
    config_dir = ctx.find_root().params.get("config_dir") or Path("config")
    with user_errors(), stores(ctx) as st:
        adapter = _master_sources()
        rows: list[MasterRow] = list(default_indices())
        failed: list[str] = []
        for source in PARSERS:
            try:
                got = _rows_for(source, from_dir, st, adapter, refresh)
            except (NiveshError, httpx.HTTPError, OSError) as e:
                failed.append(f"{source}: {e}")
                continue
            rows += got
            typer.echo(f"{source}: {len(got)} rows")
        if len(failed) == len(PARSERS):
            raise NiveshError("no master source could be read: " + "; ".join(failed))
        summary = build_master(
            st.sql, rows, load_renames(Path(config_dir) / "security_renames.yaml")
        )
    typer.echo(
        f"master: inserted {summary.inserted}, updated {summary.updated}, "
        f"upgraded {summary.upgraded}, merged {summary.merged}, renamed {summary.renamed}, "
        f"aliases {summary.aliased}, rows_dropped {summary.rows_dropped}"
    )
    for line in failed:
        typer.echo(f"failed source {line}", err=True)
    for line in summary.conflicts:
        typer.echo(f"conflict: {line}", err=True)
    for line in summary.unknown_renames:
        typer.echo(f"rename not applied: {line}", err=True)


def _india_prices() -> IndiaPrices:  # seams: tests inject MockTransport clients
    return IndiaPrices()


def _us_prices() -> UsPrices:
    return UsPrices()


def resolve_security(sql: sqlite3.Connection, query: str) -> SecurityRow:
    """One security for an ISIN/symbol/name, else an error listing up to 5 candidates."""
    master = SecurityMaster(sql)
    found = master.lookup(query)
    row = master.get(found.security_id) if found.security_id is not None else None
    if row is not None:
        return row
    if found.candidates:
        names = ", ".join(f"{c.symbol} ({c.exchange})" for c in found.candidates[:5])
        raise NiveshError(f"{query!r} is ambiguous; candidates: {names}")
    raise NiveshError(f"no security matches {query!r}; run `nivesh master build` first")


def _day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise NiveshError(f"--{name} must be an ISO date YYYY-MM-DD, got {value!r}") from None


@market_app.command("prices")
def market_prices(
    ctx: typer.Context,
    security: Annotated[str, typer.Argument(help="ISIN, symbol or company name.")],
    start: Annotated[str, typer.Option(help="First date, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option(help="Last date, YYYY-MM-DD.")],
    refresh: Annotated[bool, typer.Option(help="Bypass the price cache.")] = False,
) -> None:
    """Fetch end-of-day prices for one security, cross-check sources and report gaps."""
    with user_errors(), stores(ctx) as st:
        first, last = _day(start, "start"), _day(end, "end")
        sec = resolve_security(st.sql, security)
        rep = ingest_prices(
            st.duck, st.settings, sec, first, last, refresh=refresh,
            india=_india_prices, us=_us_prices,
        )  # fmt: skip
    flags = ", ".join(f"{k} {v}" for k, v in sorted(rep.flags.items())) or "none"
    typer.echo(f"{sec.symbol} ({sec.exchange}): bars {rep.bars}, actions {rep.actions}")
    typer.echo(f"flags: {flags}")
    typer.echo("gaps: " + (", ".join(d.isoformat() for d in rep.gaps) or "none"))
    if rep.stale:
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)


def _edgar(settings: Settings) -> Edgar:  # seam: tests inject a MockTransport client
    return Edgar(max_per_sec=settings.market.edgar_max_per_sec)


def _india_fundamentals() -> IndiaFundamentals:
    return IndiaFundamentals()


@market_app.command("fundamentals")
def market_fundamentals(
    ctx: typer.Context,
    security: Annotated[str, typer.Argument(help="ISIN, symbol or company name.")],
    refresh: Annotated[bool, typer.Option(help="Bypass the fundamentals cache.")] = False,
) -> None:
    """Fetch statements (US: SEC EDGAR; India: exchange XBRL) and, for US, filing text sections."""
    with user_errors(), stores(ctx) as st:
        sec = resolve_security(st.sql, security)
        shared = functools.cache(lambda: _edgar(st.settings))  # one rate limiter for the run
        rep = ingest_fundamentals(
            st.duck, st.sql, st.settings, sec, refresh=refresh,
            edgar=shared, india=_india_fundamentals,
        )  # fmt: skip
        fil = (
            ingest_filings(
                st.duck,
                st.sql,
                st.settings,
                sec,
                refresh=refresh,
                edgar=shared,
            )
            if sec.market == "US"
            else None
        )
    typer.echo(f"{sec.symbol} ({sec.exchange}): new rows {rep.rows}")
    typer.echo(
        f"depth: annual periods {rep.annual_periods}, quarterly periods {rep.quarterly_periods}"
    )
    if sec.market != "US":
        typer.echo(f"shareholding quarters {rep.shareholding}")
    if rep.skipped:
        typer.echo(f"skipped periods: {len(rep.skipped)}")
    if fil is not None:
        typer.echo(f"filings: {fil.listed} listed, {fil.with_sections} with text sections")
    if rep.stale or (fil and fil.stale):
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in [*rep.failed, *(fil.failed if fil else [])]:
        typer.echo(f"failed source {line}", err=True)


def _macro_fetch(settings: Settings) -> MacroFetch:  # seam: tests inject a MockTransport client
    return MacroFetch(key_ref=settings.market.fred_api_key)


@market_app.command("macro")
def market_macro(
    ctx: typer.Context,
    role: Annotated[
        list[str] | None, typer.Option(help="Role to fetch (repeatable); default all configured.")
    ] = None,
    refresh: Annotated[bool, typer.Option(help="Bypass the macro cache.")] = False,
) -> None:
    """Fetch macro series (policy rates, yields, CPI, USDINR, VIX, crude, FII/DII flows)."""
    with user_errors(), stores(ctx) as st:
        rep = ingest_macro(
            st.duck, st.settings, roles=role, refresh=refresh,
            fetch=lambda: _macro_fetch(st.settings),
        )  # fmt: skip
    for name, n in rep.new.items():
        typer.echo(f"{name}: {n} new points")
    if rep.stale:
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)


def _estimates(settings: Settings) -> Estimates:  # seam: tests inject a MockTransport client
    return Estimates(key_ref=settings.market.fmp_api_key)


@market_app.command("estimates")
def market_estimates(
    ctx: typer.Context,
    security: Annotated[str, typer.Argument(help="ISIN, symbol or company name.")],
    refresh: Annotated[bool, typer.Option(help="Bypass the estimates cache.")] = False,
) -> None:
    """Store today's analyst-estimate snapshot and the next earnings date (US only)."""
    with user_errors(), stores(ctx) as st:
        sec = resolve_security(st.sql, security)
        rep = ingest_estimates(
            st.duck, st.settings, sec, refresh=refresh,
            estimates=lambda: _estimates(st.settings),
        )  # fmt: skip
    if not rep.available:
        typer.echo(f"{sec.symbol}: estimates unavailable: {rep.reason}")
        return
    typer.echo(f"{sec.symbol} ({sec.exchange}): {rep.stored} estimate values")
    nxt = rep.next_earnings.isoformat() if rep.next_earnings else "unknown"
    typer.echo(f"next results: {nxt}")
    if rep.stale:
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)


def _feeds() -> Feeds:  # seam: tests inject a MockTransport client
    return Feeds()


@market_app.command("news")
def market_news(
    ctx: typer.Context,
    security: Annotated[
        str | None, typer.Option(help="Also list stored items tagged to this security.")
    ] = None,
    refresh: Annotated[bool, typer.Option(help="Bypass the news cache.")] = False,
) -> None:
    """Fetch the configured news and exchange-announcement feeds: tag, de-duplicate, classify."""
    with user_errors(), stores(ctx) as st:
        sec = resolve_security(st.sql, security) if security else None
        rep = ingest_news(st.duck, st.sql, st.settings, refresh=refresh, feeds=_feeds)
        shown = get_news(st.duck, security_id=sec.id, limit=10) if sec else []
    typer.echo(
        f"news: fetched {rep.fetched}, new {rep.new}, duplicates {rep.duplicates}, "
        f"untagged {rep.untagged}, undated skipped {rep.skipped_dates}, events {rep.events}"
    )
    if sec:
        typer.echo(f"{sec.symbol} ({sec.exchange}): {len(shown)} stored items")
        for n in shown:
            day = n.published_at.date().isoformat()
            typer.echo(f"  {day} {n.event_type}/{n.materiality} {n.source}")
    if rep.stale:
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)
