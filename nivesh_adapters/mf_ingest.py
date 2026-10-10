"""Mutual-fund ingest orchestration (E5). Read-only: fetch through the adapters, parse, store.

NAV: the primary source (config) is tried first; only an unavailable or rate-limited source falls
through to the fallback, a `DataQualityError` never does. A source adds only the dates it does
not already hold, so a re-run is a no-op and a late backfill lands. Gap detection counts missing
weekdays minus configured holidays (not the trading calendar, which raises for a year without
holiday data) and is recomputed and replaced on every run.
"""

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import duckdb

from nivesh_adapters.market_ingest import Run
from nivesh_adapters.mf_data import MfHoldingsClient, MfMetaClient, parse_holdings, parse_meta
from nivesh_adapters.nav import (
    AMFI_SOURCE,
    MFAPI_SOURCE,
    AmfiNavAll,
    MfapiClient,
    parse_mfapi,
    parse_navall,
)
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.market_store import get_bars
from nivesh_core.mf_models import FundHoldingRow, FundMeta, NavGap, NavPoint
from nivesh_core.mf_store import (
    get_nav,
    last_nav_date,
    latest_fund_meta,
    months_stored,
    nav_by_source,
    upsert_nav,
    write_fund_holdings,
    write_fund_meta,
    write_gaps,
)
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.consolidate import consolidate
from nivesh_engine.mf_cost import (
    CostResult,
    SchemeInfo,
    TwinResult,
    option_of,
    resolve_direct_twin,
    ter_cost,
)
from nivesh_engine.mf_overlap import DirectEquity

_SOURCE_OF = {"mfapi": MFAPI_SOURCE, "amfi": AMFI_SOURCE}


def find_nav_gaps(
    dates: Iterable[date], holidays: frozenset[date], limit: int, until: date | None = None
) -> list[NavGap]:
    """Runs of missing weekdays (holidays excluded) longer than `limit`.

    Between two consecutive stored dates, and (when `until` is given) after the last one up to
    `until`. A weekend alone is never a gap.
    """
    ds = sorted(set(dates))
    out: list[NavGap] = []

    def check(after: date, stop: date, inclusive: bool) -> None:
        miss, d = [], after + timedelta(days=1)
        while d < stop or (inclusive and d == stop):
            if d.weekday() < 5 and d not in holidays:
                miss.append(d)
            d += timedelta(days=1)
        if len(miss) > limit:
            out.append(NavGap(gap_start=miss[0], gap_end=miss[-1], missing_days=len(miss)))

    for a, b in zip(ds, ds[1:], strict=False):
        check(a, b, False)
    if ds and until is not None and until > ds[-1]:
        check(ds[-1], until, True)
    return out


@dataclass
class NavReport:
    amfi_code: str
    security_id: int
    scheme: str
    added: int = 0
    last_date: date | None = None
    source: str | None = None
    flags: list[str] = field(default_factory=list)
    gaps: list[NavGap] = field(default_factory=list)
    gap_note: str | None = None
    failed: list[str] = field(default_factory=list)
    stale: bool = False


def _points(
    run: Run,
    kind: str,
    code: str,
    today: date,
    mfapi: Callable[[], MfapiClient],
    navall: Callable[[], AmfiNavAll],
) -> list[NavPoint] | None:
    """Parsed points from one source kind; None when the source failed (recorded in run.failed)."""
    if kind == "mfapi":
        data = run.get(mfapi(), {"resource": "nav", "amfi_code": code}, "nav", label=code)
        found = None if data is None else parse_mfapi(data, today)
        if found is not None and not found:
            run.failed.append(f"nav {code}: MFapi returned no NAV points")
            return None
        return found
    data = run.get(navall(), {"resource": "navall"}, "nav", label="navall")
    if data is None:
        return None
    got = parse_navall(str(data), today)
    point = got.points.get(code)
    if point is None:
        why = "NAV is N.A." if got.skipped_na else "scheme not listed"
        run.failed.append(f"navall {code}: {why}")
        return None
    return [point]


def _flags(duck: duckdb.DuckDBPyConnection, sid: int, dates: set[date], tol: Decimal) -> list[str]:
    by_source = nav_by_source(duck, sid)
    ranked = [s for s in (MFAPI_SOURCE, AMFI_SOURCE) if s in by_source]
    out: list[str] = []
    if len(ranked) < 2:
        return out
    base, other = by_source[ranked[0]], by_source[ranked[1]]
    for d in sorted(dates & base.keys() & other.keys()):
        a, b = base[d].nav, other[d].nav
        if abs(a - b) / a > tol:
            out.append(f"{d.isoformat()}: {ranked[0]} {a} vs {ranked[1]} {b} differ beyond {tol}")
    return out


def _holidays(settings: Settings) -> frozenset[date]:
    return frozenset(d for days in settings.market.nse_holidays.values() for d in days)


def ingest_nav(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    amfi_code: str,
    *,
    refresh: bool = False,
    cross_check: bool = False,
    today: date | None = None,
    mfapi: Callable[[], MfapiClient] = MfapiClient,
    navall: Callable[[], AmfiNavAll] = AmfiNavAll,
) -> NavReport:
    """Fetch, store and gap-check the NAV history of one scheme (by AMFI code)."""
    code = amfi_code.strip()
    row = SecurityMaster(sql).by_amfi_code(code)
    if row is None:
        raise NiveshError(f"unknown AMFI code {code!r}; run `nivesh master build` first")
    day = today or ist_date(utcnow())
    cfg = settings.mf
    run = Run(duck, settings, refresh)
    rep = NavReport(code, row.id, row.name or row.symbol)

    kinds: list[str] = [cfg.sources.nav_primary]
    if cfg.sources.nav_fallback not in (cfg.sources.nav_primary, "none"):
        kinds.append(cfg.sources.nav_fallback)
    fetched: list[NavPoint] | None = None
    for kind in kinds:
        fetched = _points(run, kind, code, day, mfapi, navall)
        if fetched is not None:
            rep.source = _SOURCE_OF[kind]
            break
    if fetched is None:
        raise NiveshError(f"no NAV source answered for {code}: " + "; ".join(run.failed))

    held = nav_by_source(duck, row.id).get(rep.source or "", {})
    fresh = [p for p in fetched if p.date not in held]
    upsert_nav(duck, row.id, fresh)
    rep.added = len(fresh)
    touched = {p.date for p in fresh}
    if cross_check and len(kinds) > 1 and rep.source == _SOURCE_OF[kinds[0]]:
        extra = _points(run, kinds[1], code, day, mfapi, navall)
        if extra is not None:
            upsert_nav(duck, row.id, extra)
            touched |= {p.date for p in extra}
    rep.flags = _flags(duck, row.id, touched, cfg.nav_tolerance)

    stored = get_nav(duck, row.id)
    rep.last_date = last_nav_date(duck, row.id)
    if MFAPI_SOURCE in nav_by_source(duck, row.id):
        rep.gaps = find_nav_gaps(
            (p.date for p in stored), _holidays(settings), cfg.nav_gap_days, day
        )
    else:
        rep.gap_note = "gap check skipped: AMFI NAVAll alone gives one point per run"
    write_gaps(duck, row.id, rep.gaps)
    rep.failed, rep.stale = run.failed, run.stale
    return rep


@dataclass(frozen=True)
class HeldFund:
    """A held mutual fund keyed for the MF tables: holding -> AMFI code -> the master's row."""

    amfi_code: str
    nav_security_id: int
    name: str | None
    isin: str | None
    quantity: Decimal
    value_inr: Decimal | None


@dataclass
class Portfolio:
    """Mutual funds and direct equity from the latest holdings, with the portfolio total."""

    funds: list[HeldFund]
    skipped: list[str]
    direct: list[DirectEquity]
    total: Decimal


def load_portfolio(sql: sqlite3.Connection, settings: Settings) -> Portfolio:
    """Held mutual funds (merged per AMFI code, INR value None when unavailable) and direct equity.

    A holding on the non-preferred ISIN of a scheme maps to the same NAV row. Funds without an
    AMFI code in the master are listed in `skipped`, never guessed.
    """
    holdings = latest_holdings(sql)
    amfi_of = {h.isin: h.amfi_code for h in holdings if h.isin and h.amfi_code}
    master = SecurityMaster(sql)
    merged: dict[str, HeldFund] = {}
    skipped: list[str] = []
    direct: list[DirectEquity] = []
    book = consolidate(holdings, settings.investright.demat_ref)
    for row in book.rows:
        if row.asset_class == "equity" and row.isin:
            direct.append(DirectEquity(row.isin, row.name or row.symbol, row.value_inr))
        if row.asset_class != "mf":
            continue
        resolved = master.resolve(row.isin) if row.isin else None
        code = amfi_of.get(row.isin or "") or (resolved.amfi_code if resolved else None)
        nav_row = master.by_amfi_code(code) if code else None
        if code is None or nav_row is None:
            skipped.append(f"{row.name or row.key}: no AMFI code in the security master")
            continue
        prev = merged.get(code)
        value = row.value_inr
        if prev is not None:
            value = None if value is None or prev.value_inr is None else value + prev.value_inr
        merged[code] = HeldFund(
            code, nav_row.id, nav_row.name or row.name, row.isin,
            row.quantity + (prev.quantity if prev else Decimal(0)), value,
        )  # fmt: skip
    funds = sorted(merged.values(), key=lambda f: f.amfi_code)
    return Portfolio(funds, skipped, direct, book.total_value)


def held_funds(sql: sqlite3.Connection, settings: Settings) -> tuple[list[HeldFund], list[str]]:
    p = load_portfolio(sql, settings)
    return p.funds, p.skipped


def scheme_universe(sql: sqlite3.Connection) -> list[SchemeInfo]:
    """Every master scheme by name (AMC unknown here): the pool for direct-twin matching."""
    return [SchemeInfo(code, row.name or "") for code, row in SecurityMaster(sql).mf_schemes()]


@dataclass
class MetaReport:
    amfi_code: str
    security_id: int
    meta: FundMeta
    twin: TwinResult | None = None
    twin_meta: FundMeta | None = None
    cost: CostResult | None = None
    value_inr: Decimal | None = None
    failed: list[str] = field(default_factory=list)


def _store_meta(
    run: Run, sql: sqlite3.Connection, code: str, today: date, client: Callable[[], MfMetaClient]
) -> tuple[int, FundMeta] | None:
    row = SecurityMaster(sql).by_amfi_code(code)
    if row is None:
        raise NiveshError(f"unknown AMFI code {code!r}; run `nivesh master build` first")
    data = run.get(client(), {"resource": "meta", "amfi_code": code}, "mf_holdings", label=code)
    if data is None:
        return None
    meta = parse_meta(data, today)
    write_fund_meta(run.duck, row.id, meta)
    return row.id, meta


def ingest_meta(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    amfi_code: str,
    *,
    refresh: bool = False,
    today: date | None = None,
    meta: Callable[[], MfMetaClient] = MfMetaClient,
) -> MetaReport:
    """Store a scheme's metadata (append-only by as_of). For a regular plan, resolve its direct
    twin from the security master, ingest the twin's metadata and compute the TER cost on the
    current value of the holding."""
    code = amfi_code.strip()
    day = today or ist_date(utcnow())
    run = Run(duck, settings, refresh)
    got = _store_meta(run, sql, code, day, meta)
    if got is None:
        raise NiveshError(f"no metadata source answered for {code}: " + "; ".join(run.failed))
    rep = MetaReport(code, got[0], got[1])
    info = SchemeInfo(code, got[1].scheme_name, got[1].amc)
    if got[1].plan == "regular":
        rep.twin = resolve_direct_twin(info, scheme_universe(sql))
        direct_ter = None
        if rep.twin.status == "found" and rep.twin.amfi_code:
            twin = _store_meta(run, sql, rep.twin.amfi_code, day, meta)
            if twin is not None:
                rep.twin_meta = twin[1]
                direct_ter = twin[1].expense_ratio
        funds, _ = held_funds(sql, settings)
        rep.value_inr = next((f.value_inr for f in funds if f.amfi_code == code), None)
        rep.cost = ter_cost(got[1].expense_ratio, direct_ter, rep.value_inr, twin=rep.twin)
    rep.failed = run.failed
    return rep


@dataclass(frozen=True)
class MonthCoverage:
    """How one fund-month maps through the security master (percent of fund weight)."""

    month_end: date
    mapped_pct: Decimal
    unmapped_pct: Decimal
    other_pct: Decimal
    lines: int


@dataclass
class HoldingsReport:
    amfi_code: str
    security_id: int
    months: list[MonthCoverage] = field(default_factory=list)
    months_stored: int = 0
    coverage_note: str | None = None
    failed: list[str] = field(default_factory=list)
    stale: bool = False


MIN_MONTHS = 12


def _default_holdings(source: str, key_ref: str) -> MfHoldingsClient:
    return MfHoldingsClient(source=source, key_ref=key_ref)


def ingest_holdings(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    amfi_code: str,
    *,
    refresh: bool = False,
    holdings: Callable[[str, str], MfHoldingsClient] = _default_holdings,
) -> HoldingsReport:
    """Store the monthly portfolio the configured source offers (not capped), map each equity
    ISIN through the security master and report mapped, unmapped and other weight per month.

    Cash, derivative and debt lines are `other`, never unmapped. A line that is itself a mutual
    fund is also `other` (fund-of-fund look-through does not recurse).
    """
    code = amfi_code.strip()
    master = SecurityMaster(sql)
    fund = master.by_amfi_code(code)
    if fund is None:
        raise NiveshError(f"unknown AMFI code {code!r}; run `nivesh master build` first")
    cfg = settings.mf
    run = Run(duck, settings, refresh)
    params = {"resource": "holdings", "amfi_code": code, "source": cfg.holdings_source}
    data = run.get(holdings(cfg.holdings_source, cfg.holdings_api_ref), params, "mf_holdings", code)
    if data is None:
        raise NiveshError(f"no holdings source answered for {code}: " + "; ".join(run.failed))
    parsed = parse_holdings(data, cfg.holdings_source, code)

    ids: dict[str, SecurityRow | None] = {}

    def row_of(isin: str) -> SecurityRow | None:
        if isin not in ids:
            found = master.by_isin(isin)
            ids[isin] = found[0] if found else None
        return ids[isin]

    rep = HoldingsReport(code, fund.id)
    stored: list[FundHoldingRow] = []
    for month in sorted(parsed):
        mapped = unmapped = other = Decimal(0)
        for r in parsed[month]:
            hit = row_of(r.isin) if r.kind == "equity" else None
            if r.kind == "equity" and hit is not None and hit.asset_class == "mf":
                r = r.model_copy(update={"kind": "other", "holding_security_id": hit.id})
            elif hit is not None:
                r = r.model_copy(update={"holding_security_id": hit.id})
            stored.append(r)
            if r.kind == "other":
                other += r.weight_pct
            elif r.holding_security_id is None:
                unmapped += r.weight_pct
            else:
                mapped += r.weight_pct
        rep.months.append(MonthCoverage(month, mapped, unmapped, other, len(parsed[month])))
    write_fund_holdings(duck, fund.id, stored)
    rep.months_stored = len(months_stored(duck, fund.id))
    if rep.months_stored < MIN_MONTHS:
        rep.coverage_note = (
            f"coverage: {rep.months_stored} of {MIN_MONTHS} months stored; the source offers fewer"
        )
    rep.failed, rep.stale = run.failed, run.stale
    return rep


@dataclass
class FundInputs:
    """Stored series for one scheme, ready for `analyse_fund`."""

    amfi_code: str
    security_id: int
    name: str
    nav: list[tuple[date, Decimal]]
    option: str | None
    meta: FundMeta | None
    benchmark: list[tuple[date, Decimal]] | None = None
    benchmark_symbol: str | None = None
    benchmark_reason: str | None = None


def _benchmark(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    meta: FundMeta | None, override: str | None,
) -> tuple[list[tuple[date, Decimal]] | None, str | None, str | None]:  # fmt: skip
    """(series, symbol, reason). Mapped through `mf.benchmarks` (fund_meta.benchmark text to an
    index symbol); an unmapped benchmark makes relative metrics unavailable, never zero."""
    text = meta.benchmark if meta else None
    symbol = override or (settings.mf.benchmarks.get(text) if text else None)
    if symbol is None:
        if text:
            return None, None, f"benchmark {text!r} is not mapped under mf.benchmarks"
        return None, None, "benchmark unknown (no stored metadata; run `nivesh mf meta`)"
    rows = [r for r in SecurityMaster(sql).by_symbol(symbol) if r.asset_class == "index"]
    if not rows:
        return None, symbol, f"index {symbol!r} is not in the security master"
    bars = get_bars(duck, rows[0].id)
    if not bars:
        return None, symbol, f"no stored bars for index {symbol!r}; run `nivesh market prices`"
    return [(b.date, b.close) for b in bars], symbol, None


def load_fund_inputs(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    amfi_code: str,
    *,
    benchmark: str | None = None,
) -> FundInputs:
    code = amfi_code.strip()
    row = SecurityMaster(sql).by_amfi_code(code)
    if row is None:
        raise NiveshError(f"unknown AMFI code {code!r}; run `nivesh master build` first")
    nav = [(p.date, p.nav) for p in get_nav(duck, row.id)]
    if not nav:
        raise NiveshError(f"no stored NAV for {code}; run `nivesh mf nav {code}`")
    meta = latest_fund_meta(duck, row.id)
    series, symbol, reason = _benchmark(duck, sql, settings, meta, benchmark)
    option = meta.option if meta else option_of(row.name or "")
    return FundInputs(
        code, row.id, row.name or row.symbol, nav, option, meta, series, symbol, reason
    )
