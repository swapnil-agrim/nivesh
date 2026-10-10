"""The one bridge between the stores and the pure analysis engines (ST-6.x). Read-only.

Engines take plain values; this module reads them, in bulk (a constant number of statements for
any number of securities), and hands them over. It is not an adapter: it has no network, no client
and no credential, and it never writes. Bars are always returned on the adjusted basis (splits and
bonuses applied, dividends not), with Yahoo closes never adjusted twice; passing an end date shows
only what was known then, including which corporate actions had gone ex.
"""

import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import duckdb

from nivesh_adapters.mf_ingest import HeldFund, Portfolio, load_portfolio
from nivesh_adapters.mf_report import scheme_isins
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.config import Settings
from nivesh_core.holdings import Lot, Txn
from nivesh_core.holdings_store import latest_holdings, latest_lots, list_transactions
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.market_store import (
    EstimateRow,
    estimate_history_many,
    filing_items_many,
    get_bars_many,
    get_corp_actions_many,
    get_macro,
    get_shareholding_many,
    get_statement_rows_many,
    latest_estimates_many,
)
from nivesh_core.mf_models import FundHoldingRow
from nivesh_core.mf_store import get_fund_holdings, get_nav, latest_fund_meta, months_stored
from nivesh_core.security_master import Peers, SecurityMaster, SecurityRow
from nivesh_engine.adjust import split_factor_map
from nivesh_engine.bars import Bar, to_bars
from nivesh_engine.consolidate import Row, consolidate
from nivesh_engine.fa import sector_kind_of
from nivesh_engine.fx import VALUATION_MAX_AGE_DAYS, FxResult, rate_on_or_before, unavailable
from nivesh_engine.metrics import SecurityInputs
from nivesh_engine.mf_lots import fifo_lots, valued_lots
from nivesh_engine.mf_overlap import LookThrough, OwnedFund, look_through
from nivesh_engine.redflags import EightK, FlagInputs
from nivesh_engine.returns import MAX_LOT_RATE_AGE_DAYS
from nivesh_engine.risk import RiskCandidate, RiskHolding
from nivesh_engine.statements import StatementRow
from nivesh_engine.valuation import (
    MULTIPLES,
    PeerMultiples,
    ValuationInputs,
    current_multiples,
    valuation_multiples,
)
from nivesh_engine.xray import HoldingFlows, mf_holding_flows, us_holding_flows


@dataclass(frozen=True)
class IndexRef:
    """A configured index resolved to a security id, or why it could not be."""

    security_id: int | None
    symbol: str | None
    reason: str | None = None


def load_bars(
    duck: duckdb.DuckDBPyConnection,
    security_ids: Sequence[int],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[int, list[Bar]]:
    """Adjusted bars per security, oldest first, for the given date window. A security with no
    bars maps to an empty list."""
    stored = get_bars_many(duck, security_ids, start, end)
    actions = get_corp_actions_many(duck, security_ids)
    out: dict[int, list[Bar]] = {}
    for sid, bars in stored.items():
        known = [a for a in actions[sid] if end is None or a.ex_date <= end]
        bars_out, _ = to_bars(bars, split_factor_map(bars, known))
        out[sid] = bars_out
    return out


def load_statements(
    duck: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date | None = None
) -> dict[int, list[StatementRow]]:
    """Every filed version of every statement row filed on or before `as_of`."""
    return get_statement_rows_many(duck, security_ids, as_of)


def load_shareholding(
    duck: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date | None = None
) -> dict[int, list[ShareholdingRow]]:
    return get_shareholding_many(duck, security_ids, as_of)


def load_estimates(
    duck: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date | None = None
) -> dict[int, list[EstimateRow]]:
    return latest_estimates_many(duck, security_ids, as_of)


def security_meta(sql: sqlite3.Connection, security_ids: Sequence[int]) -> dict[int, SecurityRow]:
    """Master rows (market, sector, industry, currency) for the ids that exist."""
    return SecurityMaster(sql).get_many(security_ids)


def load_peers(
    sql: sqlite3.Connection, security_id: int, cfg: AnalysisSettings, *, limit: int | None = None
) -> Peers:
    """Peers by industry, or the owner's override from `analysis.valuation.peer_overrides`."""
    return SecurityMaster(sql).peers(security_id, cfg.valuation.peer_overrides, limit=limit)


def _index(sql: sqlite3.Connection, name: str, where: str) -> IndexRef:
    row = sql.execute(
        "SELECT id, symbol FROM security WHERE asset_class = 'index' AND unresolved = 0 "
        "AND UPPER(symbol) = UPPER(?) ORDER BY id",
        (name.strip(),),
    ).fetchone()
    if row is None:
        return IndexRef(None, name, f"index {name!r} ({where}) is not in the security master")
    return IndexRef(int(row[0]), str(row[1]))


def benchmark_for(sql: sqlite3.Connection, sec: SecurityRow, cfg: AnalysisSettings) -> IndexRef:
    """The market benchmark index for a security, from `analysis.ta.benchmarks`."""
    name = cfg.ta.benchmarks.get(sec.market)
    if not name:
        reason = f"no benchmark configured for market {sec.market} (analysis.ta.benchmarks)"
        return IndexRef(None, None, reason)
    return _index(sql, name, "analysis.ta.benchmarks")


def sector_index_for(sql: sqlite3.Connection, sec: SecurityRow, cfg: AnalysisSettings) -> IndexRef:
    """The sector index for a security, from `analysis.ta.sector_index`."""
    if not sec.sector:
        return IndexRef(None, None, "no sector recorded for this security")
    name = cfg.ta.sector_index.get(sec.sector)
    if not name:
        reason = f"no index configured for sector {sec.sector!r} (analysis.ta.sector_index)"
        return IndexRef(None, None, reason)
    return _index(sql, name, "analysis.ta.sector_index")


@dataclass(frozen=True)
class ClosePoints:
    """Raw closes for valuation: the last bar of each calendar month, and the latest bar."""

    month_ends: tuple[tuple[date, Decimal], ...]
    last: tuple[date, Decimal] | None


LAST_CLOSE_LOOKBACK_DAYS = 45
AUDITOR_ITEM = "item_4.01"


def _last_day_of_month(d: date) -> bool:
    return (d + timedelta(days=1)).month != d.month


def load_closes(
    duck: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date, *, years: int
) -> dict[int, ClosePoints]:
    """Raw (unadjusted) closes: the last bar of each calendar month over `years` years, and the
    latest bar, both on or before `as_of`. The raw close is on the share basis of the statements
    filed at the time, which is what a historical P/E needs. The month containing `as_of` is only
    a month end when `as_of` is the last day of that month."""
    start = as_of - timedelta(days=366 * years + LAST_CLOSE_LOOKBACK_DAYS)
    stored = get_bars_many(duck, security_ids, start, as_of)
    out: dict[int, ClosePoints] = {}
    for sid, bars in stored.items():
        by_month: dict[tuple[int, int], tuple[date, Decimal]] = {}
        for b in bars:  # oldest first, so the last bar of a month wins
            by_month[(b.date.year, b.date.month)] = (b.date, b.close)
        partial = (as_of.year, as_of.month) if not _last_day_of_month(as_of) else None
        ends = tuple(v for k, v in sorted(by_month.items()) if k != partial) if years else ()
        last = (bars[-1].date, bars[-1].close) if bars else None
        out[sid] = ClosePoints(ends, last)
    return out


def load_valuation_inputs(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    security_ids: Sequence[int],
    as_of: date,
    cfg: AnalysisSettings,
    *,
    history: bool = True,
) -> dict[int, ValuationInputs]:
    """Valuation inputs for the securities that exist: every statement row filed on or before
    `as_of`, month-end and latest raw closes, market and sector kind. `history=False` skips the
    month-end series (enough for a current multiple)."""
    meta = security_meta(sql, security_ids)
    ids = [i for i in security_ids if i in meta]
    rows = load_statements(duck, ids, as_of)
    years = max(cfg.valuation.history_years) if history else 0
    closes = load_closes(duck, ids, as_of, years=years)
    return {
        i: ValuationInputs(
            market=meta[i].market,
            sector_kind=sector_kind_of(meta[i].sector, rows[i], cfg),
            as_of=as_of,
            rows=tuple(rows[i]),
            month_ends=closes[i].month_ends,
            last_close=closes[i].last,
        )
        for i in ids
    }


def load_peer_multiples(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    security_id: int,
    as_of: date,
    cfg: AnalysisSettings,
) -> PeerMultiples:
    """The current multiples of the peers (industry or owner override), by multiple name,
    sorted by peer symbol. A peer without a meaningful value for a multiple is left out of it."""
    found = load_peers(sql, security_id, cfg)
    ids = [p.id for p in found.rows]
    inputs = load_valuation_inputs(duck, sql, ids, as_of, cfg, history=False)
    values: dict[str, list[Decimal]] = {m: [] for m in MULTIPLES}
    for p in found.rows:
        for name, metric in current_multiples(inputs[p.id]).items():
            if metric.available and metric.value is not None:
                values[name].append(metric.value)
    return PeerMultiples(
        {k: tuple(v) for k, v in values.items()},
        considered=len(found.rows),
        reason=found.reason,
        symbols=tuple(p.symbol for p in found.rows),
    )


def load_auditor_filings(
    duck: duckdb.DuckDBPyConnection,
    security_ids: Sequence[int],
    as_of: date,
    cfg: AnalysisSettings,
) -> dict[int, tuple[EightK, ...]]:
    """The stored 8-K filings (with stored text sections) in the auditor look-back ending at
    `as_of`, newest first, each marked when it carries Item 4.01 (a change of certifying
    accountant). A security with none maps to an empty tuple: nothing stored, not a clean record."""
    since = as_of - timedelta(days=cfg.flags.auditor_lookback_days)
    found = filing_items_many(duck, security_ids, ["8-K"], since, as_of)
    return {
        sid: tuple(EightK(f.filing_id, f.filed_at, AUDITOR_ITEM in f.sections) for f in items)
        for sid, items in found.items()
    }


def load_flag_inputs(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    security_id: int,
    as_of: date,
    cfg: AnalysisSettings,
    *,
    contingent_liabilities: Decimal | None = None,
) -> FlagInputs | None:
    """Red-flag inputs for one security as of a date (None for an unknown security): statement
    rows and shareholding filed by then, and for a US security its stored 8-K filings. India has
    no auditor source, so that input is None. Contingent liabilities come from the caller."""
    meta = security_meta(sql, [security_id]).get(security_id)
    if meta is None:
        return None
    auditor = None
    if meta.market == "US":
        auditor = load_auditor_filings(duck, [security_id], as_of, cfg)[security_id]
    return FlagInputs(
        market=meta.market,
        rows=tuple(load_statements(duck, [security_id], as_of)[security_id]),
        shareholding=tuple(load_shareholding(duck, [security_id], as_of)[security_id]),
        auditor=auditor,
        contingent_liabilities=contingent_liabilities,
    )


# ---- inputs for the screener, the X-ray and the risk metrics ------------------------------------
@dataclass(frozen=True)
class XrayInputs:
    rows: tuple[Row, ...]
    sector_of: dict[str, str | None]
    market_cap_of: dict[str, Decimal | None]
    mf_category_of: dict[str, str | None]
    market_of: dict[str, str]
    flows: dict[str, HoldingFlows]
    look_through: LookThrough | None
    notes: tuple[str, ...]
    valuation: date


@dataclass(frozen=True)
class RiskInputs:
    holdings: tuple[RiskHolding, ...]
    candidate: RiskCandidate | None
    bars: dict[int, list[Bar]]
    benchmarks: dict[str, list[Bar]]
    notes: tuple[str, ...]


BARS_LOOKBACK_DAYS = 450  # about 310 sessions: enough for the 253 bars the 12-month return needs
USDINR: Literal["usdinr"] = "usdinr"


def _benchmark_bars(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    meta: Mapping[int, SecurityRow],
    cfg: AnalysisSettings,
    start: date,
    end: date,
    *,
    sectors: bool,
) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    """Index bars by market, and (when asked) by sector name, for the configured indices."""
    market_ref: dict[str, int] = {}
    sector_ref: dict[str, int] = {}
    for sec in sorted(meta.values(), key=lambda r: r.id):
        if sec.market not in market_ref:
            ref = benchmark_for(sql, sec, cfg)
            if ref.security_id is not None:
                market_ref[sec.market] = ref.security_id
        if sectors and sec.sector and sec.sector not in sector_ref:
            found = sector_index_for(sql, sec, cfg)
            if found.security_id is not None:
                sector_ref[sec.sector] = found.security_id
    wanted = sorted({*market_ref.values(), *sector_ref.values()})
    loaded = load_bars(duck, wanted, start=start, end=end) if wanted else {}
    return (
        {m: loaded[i] for m, i in market_ref.items()},
        {s: loaded[i] for s, i in sector_ref.items()},
    )


def load_screen_inputs(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    security_ids: Sequence[int],
    as_of: date,
    cfg: AnalysisSettings,
    *,
    needs: Collection[str],
) -> dict[int, SecurityInputs]:
    """Screener inputs for the securities that exist, reading only what `needs` names (see
    `metrics.inputs_needed`): `bars`, `benchmark`, `sector_bars`, `statements`, `shareholding`,
    `last_close`, `closes`, `estimates`, `filings`, `peers`. The number of statements is the same
    for ten securities as for a thousand, apart from `peers`, which reads per security."""
    want = set(needs)
    meta = security_meta(sql, security_ids)
    ids = sorted(meta)
    start = as_of - timedelta(days=BARS_LOOKBACK_DAYS)
    bars = load_bars(duck, ids, start=start, end=as_of) if "bars" in want else {}
    by_market, by_sector = (
        _benchmark_bars(duck, sql, meta, cfg, start, as_of, sectors="sector_bars" in want)
        if want & {"benchmark", "sector_bars"}
        else ({}, {})
    )
    rows = load_statements(duck, ids, as_of) if "statements" in want else {}
    share = load_shareholding(duck, ids, as_of) if "shareholding" in want else {}
    history = max(cfg.valuation.history_years) if "closes" in want else 0
    closes = load_closes(duck, ids, as_of, years=history) if want & {"closes", "last_close"} else {}
    since = as_of - timedelta(days=2 * cfg.screen.revision_lookback_days)
    estimates = estimate_history_many(duck, ids, since, as_of) if "estimates" in want else {}
    us_ids = [i for i in ids if meta[i].market == "US"]
    auditors = (
        load_auditor_filings(duck, us_ids, as_of, cfg) if "filings" in want and us_ids else {}
    )
    out: dict[int, SecurityInputs] = {}
    for sid in ids:
        m = meta[sid]
        mine = tuple(rows.get(sid, ()))
        mine_share = tuple(share.get(sid, ()))
        kind = sector_kind_of(m.sector, mine, cfg)
        valuation = None
        if sid in closes:
            valuation = ValuationInputs(
                m.market, kind, as_of, mine, closes[sid].month_ends, closes[sid].last
            )
        flags = (
            FlagInputs(m.market, mine, mine_share, auditors.get(sid) if m.market == "US" else None)
            if "filings" in want
            else None
        )
        peers = load_peer_multiples(duck, sql, sid, as_of, cfg) if "peers" in want else None
        out[sid] = SecurityInputs(
            sid, m.symbol, m.market, m.sector, kind, bars.get(sid, ()),
            by_market.get(m.market), by_sector.get(m.sector or ""), mine, mine_share, valuation,
            peers, tuple(estimates.get(sid, ())), flags,
        )  # fmt: skip
    return out


@dataclass(frozen=True)
class _Book:
    """The consolidated holdings with the rate used for the foreign ones."""

    rows: tuple[Row, ...]
    fx: FxResult
    obs: list[tuple[date, Decimal]]
    source: str
    notes: tuple[str, ...]


def _book(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, valuation: date
) -> _Book:
    holdings = latest_holdings(sql)
    first = min((x.acquired_on for x in latest_lots(sql)), default=valuation)
    since = min(valuation - timedelta(days=400), first - timedelta(days=MAX_LOT_RATE_AGE_DAYS + 10))
    obs = get_macro(duck, USDINR, since, valuation)
    spec = settings.market.macro_series.get(USDINR)
    source = f"{spec.source}:{spec.id}" if spec else USDINR
    fx = (
        rate_on_or_before(obs, valuation, source, max_age_days=VALUATION_MAX_AGE_DAYS)
        if obs
        else unavailable("no USDINR series stored", source)
    )
    book = consolidate(holdings, settings.investright.demat_ref, fx)
    return _Book(tuple(book.rows), fx, obs, source, tuple(book.notes))


def _find(master: SecurityMaster, row: Row) -> SecurityRow | None:
    """The master row of a consolidated holding: by ISIN, else by symbol and exchange."""
    if row.isin:
        hit = master.by_isin(row.isin)
        if hit:
            return hit[0]
    named = master.by_symbol(row.symbol)
    return next((h for h in named if h.exchange == row.exchange), named[0] if named else None)


def _market_cap(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, sec: SecurityRow, valuation: date,
    cfg: AnalysisSettings,
) -> Decimal | None:  # fmt: skip
    """Shares outstanding times the latest stored close, in the security's own currency."""
    inputs = load_valuation_inputs(duck, sql, [sec.id], valuation, cfg, history=False)
    return valuation_multiples(inputs[sec.id], None, cfg=cfg).market_cap


def _mf_flows(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    fund: HeldFund,
    txns: Sequence[Txn],
    valuation: date,
) -> HoldingFlows:
    isins = scheme_isins(sql, fund.amfi_code)
    mine = [t for t in txns if t.amfi_code == fund.amfi_code or t.isin in isins]
    points = get_nav(duck, fund.nav_security_id, end=valuation)
    latest = points[-1] if points else None
    valued = valued_lots(
        fifo_lots(mine), latest.nav if latest else None, latest.date if latest else None,
        valuation, settings.mf.max_nav_age_days,
    )  # fmt: skip
    return mf_holding_flows(valued, fund.quantity, valuation)


def _look_through(
    duck: duckdb.DuckDBPyConnection, master: SecurityMaster, book: Portfolio
) -> LookThrough | None:
    stored: dict[str, list[FundHoldingRow]] = {}
    for f in book.funds:
        months = months_stored(duck, f.nav_security_id)
        if months:
            stored[f.amfi_code] = get_fund_holdings(duck, f.nav_security_id, months[-1])
    if not stored or book.total <= 0:
        return None
    isins = {r.isin for lines in stored.values() for r in lines if r.kind == "equity"}
    isins |= {d.isin for d in book.direct}
    sectors = {i: (found[0].sector if (found := master.by_isin(i)) else None) for i in isins}
    owned = [OwnedFund(f.amfi_code, f.name or f.amfi_code, f.value_inr) for f in book.funds]
    return look_through(owned, stored, sectors, book.direct, book.total)


def load_xray_inputs(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, valuation: date
) -> XrayInputs:
    """Everything `portfolio_xray` needs from the stores: the consolidated rows (USD rows at the
    valuation-date rate), sector, market and market cap by row, fund category, dated flows per row
    (US lots, mutual-fund lots), and the look-through. Indian direct equity has no dated lots, so
    it has no flows entry."""
    cfg = settings.analysis
    held = _book(duck, sql, settings, valuation)
    master = SecurityMaster(sql)
    portfolio = load_portfolio(sql, settings)
    by_isin = {f.isin: f for f in portfolio.funds if f.isin}
    lots: dict[tuple[str, str], list[Lot]] = {}
    for x in latest_lots(sql):
        lots.setdefault((x.symbol, x.exchange), []).append(x)
    txns = list_transactions(sql)
    sector_of: dict[str, str | None] = {}
    cap_of: dict[str, Decimal | None] = {}
    category_of: dict[str, str | None] = {}
    market_of: dict[str, str] = {}
    flows: dict[str, HoldingFlows] = {}
    for row in held.rows:
        sec = _find(master, row)
        if sec is not None:
            sector_of[row.key] = sec.sector
            market_of[row.key] = sec.market
        fund = by_isin.get(row.isin or "") if row.asset_class == "mf" else None
        if fund is not None:
            fmeta = latest_fund_meta(duck, fund.nav_security_id)
            category_of[row.key] = fmeta.category if fmeta else None
            flows[row.key] = _mf_flows(duck, sql, settings, fund, txns, valuation)
        elif row.currency == "USD":
            terminal = row.value_native if row.value_inr is not None else None
            mine = lots.get((row.symbol, row.exchange), [])
            if terminal is not None and mine:
                flows[row.key] = us_holding_flows(
                    mine, row.quantity, terminal, valuation, held.obs, held.source
                )
            elif mine:
                flows[row.key] = HoldingFlows((), "none", held.fx.reason or "no USDINR rate")
        if sec is not None and sec.market == "US" and row.asset_class == "equity":
            cap_of[row.key] = _market_cap(duck, sql, sec, valuation, cfg)
    notes = (*held.notes, *portfolio.skipped)
    return XrayInputs(
        held.rows, sector_of, cap_of, category_of, market_of, flows,
        _look_through(duck, master, portfolio), notes, valuation,
    )  # fmt: skip


def load_risk_inputs(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    valuation: date,
    *,
    candidate: str | None = None,
    proposed_weight_pct: Decimal | None = None,
) -> RiskInputs:
    """Holdings with price bars (direct securities; mutual funds have NAV, not bars, and are named
    in the notes), the market benchmarks, and the candidate when one is named with its weight."""
    if candidate is not None and proposed_weight_pct is None:
        raise ValueError("a candidate needs a proposed weight")
    cfg = settings.analysis
    held = _book(duck, sql, settings, valuation)
    master = SecurityMaster(sql)
    notes = list(held.notes)
    rate = held.fx.rate

    def per_unit(currency: str) -> Decimal:
        return rate if currency == "USD" and rate is not None else Decimal(1)

    holdings: list[RiskHolding] = []
    funds = 0
    for row in held.rows:
        if row.asset_class == "mf":
            funds += 1
            continue
        sec = _find(master, row)
        if sec is None or row.value_inr is None:
            continue
        holdings.append(
            RiskHolding(
                sec.id,
                row.name or row.symbol,
                row.value_inr,
                sec.market,
                sec.sector,
                per_unit(row.currency),
            )  # fmt: skip
        )
    if funds:
        notes.append(f"{funds} mutual fund holding(s) have NAV, not price bars, and are left out")
    cand = None
    meta = {h.security_id: master.get(h.security_id) for h in holdings}
    if candidate is not None and proposed_weight_pct is not None:
        found = master.lookup(candidate)
        sec = master.get(found.security_id) if found.security_id is not None else None
        if sec is None:
            raise ValueError(f"unknown candidate {candidate!r}")
        cand = RiskCandidate(
            sec.id, sec.name or sec.symbol, proposed_weight_pct, sec.market, sec.sector,
            per_unit(sec.currency),
        )  # fmt: skip
        meta[sec.id] = sec
    ids = sorted(meta)
    start = valuation - timedelta(days=366 * max(cfg.risk.drawdown_years) + 90)
    bars = load_bars(duck, ids, start=start, end=valuation) if ids else {}
    rows = {i: m for i, m in meta.items() if m is not None}
    benches, _ = _benchmark_bars(duck, sql, rows, cfg, start, valuation, sectors=False)
    for market in sorted({m.market for m in rows.values()} - set(benches)):
        sample = next(m for m in rows.values() if m.market == market)
        notes.append(f"no benchmark for market {market}: {benchmark_for(sql, sample, cfg).reason}")
    return RiskInputs(tuple(holdings), cand, bars, benches, tuple(notes))
