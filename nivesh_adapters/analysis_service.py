"""Read-only assembly of the analysis engines, shared by the `nivesh` CLI and the `engine` MCP
server. One function per engine tool: each takes open connections, the settings and (for portfolio
tools) the profile, never a CLI context, and returns plain data. Nothing is fetched or written.
"""

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb
from pydantic import BaseModel

from nivesh_adapters.analysis_data import (
    load_flag_inputs,
    load_peer_multiples,
    load_risk_inputs,
    load_screen_inputs,
    load_valuation_inputs,
    load_xray_inputs,
)
from nivesh_adapters.mf_ingest import FundInputs, Portfolio, load_fund_inputs, load_portfolio
from nivesh_adapters.mf_report import FundReport, ValuationReport, fund_valuation, run_doctor
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.mf_models import FundMeta
from nivesh_core.mf_store import get_fund_holdings, latest_fund_meta, months_stored
from nivesh_core.profile import Profile
from nivesh_core.security_master import Lookup, SecurityMaster, SecurityRow
from nivesh_engine.committee_rules import RiskFacts
from nivesh_engine.metrics import Bundles
from nivesh_engine.mf_overlap import (
    LookThrough,
    OverlapMatrix,
    OwnedFund,
    look_through,
    overlap_matrix,
)
from nivesh_engine.mf_returns import FundAnalytics, analyse_fund
from nivesh_engine.redflags import any_hard, detect_flags
from nivesh_engine.risk import risk_metrics
from nivesh_engine.scoring import ScoreCard, ranking, raw_inputs, score_universe
from nivesh_engine.universe import matches_exclusion
from nivesh_engine.valuation import valuation_multiples, valuation_range
from nivesh_engine.xray import portfolio_xray

HORIZONS = ("long_term", "positional")
# `key` (a holding's row id) and `portfolio_vol` contain words a redaction pass keyed on field
# names (nivesh_core.redact) blanks; they are renamed at this boundary.
RENAMED = {"key": "ref", "portfolio_vol": "overall_vol"}
Number = Callable[[Decimal], Any]


def _exact(d: Decimal) -> str:
    return format(d, "f")


def plain(obj: object, number: Number = _exact) -> Any:
    """A JSON-ready copy: decimals through `number` (exact strings by default), dates as ISO
    text, dataclasses as mappings."""
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, Decimal):
        return number(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return plain(obj.model_dump(), number)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            RENAMED.get(f.name, f.name): plain(getattr(obj, f.name), number) for f in fields(obj)
        }
    if isinstance(obj, Mapping):
        return {str(k): plain(v, number) for k, v in obj.items()}
    if isinstance(obj, set | frozenset):
        return sorted((plain(v, number) for v in obj), key=str)
    if isinstance(obj, Sequence):
        return [plain(v, number) for v in obj]
    return str(obj)


_PREFIX = re.compile(r"^(?P<tag>[A-Za-z]+):(?P<rest>.+)$")
_PREFIXES = {"NSE": ("IN", "NSE"), "US": ("US", None)}  # tag -> (market, exchange)


def _prefixed(query: str) -> tuple[str, str | None, str | None]:
    """(query, market, exchange) once an `NSE:`/`US:` tag is read; `id:<n>` is left alone."""
    m = _PREFIX.match(query.strip())
    if m is None or m["tag"].lower() == "id":
        return query, None, None
    tag = m["tag"].upper()
    if tag not in _PREFIXES:
        raise NiveshError(f"unknown prefix {m['tag']!r}; use NSE:SYMBOL or US:SYMBOL")
    return (m["rest"].strip(), *_PREFIXES[tag])


def resolve_security(sql: sqlite3.Connection, query: str) -> SecurityRow:
    """One security for an ISIN/symbol/name, else an error listing up to 5 candidates.
    `NSE:SYMBOL` picks the Indian NSE listing and `US:SYMBOL` the US one."""
    text, market, exchange = _prefixed(query)
    master = SecurityMaster(sql)
    found = master.lookup(text, market)
    cands = [c for c in found.candidates if exchange is None or c.exchange == exchange]
    if exchange is None:
        sid = found.security_id
    else:  # an exact symbol/ISIN hit narrowed to the named exchange
        exact = found.matched_by in ("symbol", "isin", "id")
        sid = cands[0].security_id if exact and len(cands) == 1 else None
    found = Lookup(sid, found.matched_by, cands)
    row = master.get(found.security_id) if found.security_id is not None else None
    if row is not None:
        return row
    if cands:
        names = ", ".join(f"{c.symbol} ({c.exchange})" for c in cands[:5])
        hint = "; prefix the ticker with NSE: or US: to pick a listing" if market is None else ""
        raise NiveshError(f"{query!r} is ambiguous; candidates: {names}{hint}")
    raise NiveshError(f"no security matches {query!r}; run `nivesh master build` first")


@dataclass(frozen=True)
class SecReport:
    security_id: int
    symbol: str
    result: Any


def _inputs(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, query: str,
    day: date, needs: set[str],
) -> Any:  # fmt: skip
    sec = resolve_security(sql, query)
    found = load_screen_inputs(duck, sql, [sec.id], day, settings.analysis, needs=needs)
    return sec, found[sec.id]


def ta_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, query: str,
    day: date,
) -> SecReport:  # fmt: skip
    """Technical indicators, support and resistance, and the setup, from stored daily bars."""
    sec, inp = _inputs(duck, sql, settings, query, day, {"bars", "benchmark", "sector_bars"})
    b = Bundles(inp, day, settings.analysis)
    return SecReport(sec.id, sec.symbol, {"indicators": b.ta(), "setup": b.setup()})


def fa_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, query: str,
    day: date,
) -> SecReport:  # fmt: skip
    """Growth, profitability, balance sheet and cash quality, using filings up to the date."""
    sec, inp = _inputs(duck, sql, settings, query, day, {"statements", "shareholding"})
    return SecReport(sec.id, sec.symbol, Bundles(inp, day, settings.analysis).fa())


def valuation_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, query: str,
    day: date,
) -> SecReport:  # fmt: skip
    """Multiples with history, percentile and peer median, plus the reverse-DCF range."""
    cfg = settings.analysis
    sec = resolve_security(sql, query)
    inputs = load_valuation_inputs(duck, sql, [sec.id], day, cfg)[sec.id]
    peers = load_peer_multiples(duck, sql, sec.id, day, cfg)
    result = {
        "multiples": valuation_multiples(inputs, peers, cfg=cfg),
        "range": valuation_range(inputs, cfg=cfg),
    }
    return SecReport(sec.id, sec.symbol, result)


def flag_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, query: str,
    day: date,
) -> SecReport:  # fmt: skip
    """Red flags with status, severity and evidence; a flag that cannot be tested says so."""
    sec = resolve_security(sql, query)
    inputs = load_flag_inputs(duck, sql, sec.id, day, settings.analysis)
    if inputs is None:
        raise NiveshError(f"{query!r} is not in the security master")
    flags = detect_flags(inputs, as_of=day, cfg=settings.analysis)
    return SecReport(sec.id, sec.symbol, {"flags": flags})


def xray_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, day: date,
) -> dict[str, Any]:  # fmt: skip
    """Allocation, drift, concentration, limits, look-through and XIRR of the stored holdings."""
    inp = load_xray_inputs(duck, sql, settings, day)
    xray = portfolio_xray(
        inp.rows, profile, sector_of=inp.sector_of, market_cap_of=inp.market_cap_of,
        mf_category_of=inp.mf_category_of, flows=inp.flows, look_through=inp.look_through,
        cfg=settings.analysis.xray, market_of=inp.market_of,
    )  # fmt: skip
    return {"xray": xray, "notes": inp.notes}


def parse_weight(weight: str) -> Decimal:
    try:
        pct = Decimal(weight)
    except ArithmeticError:
        raise NiveshError(f"--weight must be a number, got {weight!r}") from None
    if not pct.is_finite() or not Decimal(0) < pct < Decimal(100):
        raise NiveshError("--weight must be between 0 and 100 (percent)")
    return pct


def risk_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, day: date, candidate: str | None, weight: str | None,
) -> dict[str, Any]:  # fmt: skip
    """Volatility, drawdown, beta, days to trade and the pro-forma weights with a candidate."""
    if (candidate is None) != (weight is None):
        raise NiveshError("--candidate and --weight go together")
    pct = None if weight is None else parse_weight(weight)
    try:
        inp = load_risk_inputs(
            duck, sql, settings, day, candidate=candidate, proposed_weight_pct=pct
        )
    except ValueError as e:
        raise NiveshError(str(e)) from None
    res = risk_metrics(
        inp.holdings, inp.candidate, inp.bars, inp.benchmarks, cfg=settings.analysis.risk,
        as_of=day, max_position_pct=Decimal(str(profile.max_position_pct)),
        max_sector_pct=Decimal(str(profile.max_sector_pct)),
    )  # fmt: skip
    return {"risk": res, "notes": inp.notes}


def universe_ids(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, symbols: Sequence[str]
) -> tuple[list[int], str]:
    """The securities to read: the ones named, else every direct security with stored bars."""
    if symbols:
        ids = sorted({resolve_security(sql, s).id for s in symbols})
        return ids, f"explicit list of {len(ids)} securities named on the command line"
    rows = duck.execute("SELECT DISTINCT security_id FROM price_bar").fetchall()
    found = SecurityMaster(sql).get_many([int(x[0]) for x in rows])
    ids = sorted(i for i, s in found.items() if s.asset_class not in ("mf", "index"))
    return (
        ids,
        f"all {len(ids)} securities with stored bars (no investable universe is defined yet)",
    )


_SCORE_NEEDS = {"bars", "benchmark", "sector_bars", "statements", "shareholding", "closes",
                "last_close", "estimates", "filings", "peers"}  # fmt: skip


@dataclass(frozen=True)
class ScoreReport:
    horizon: str
    basis: str
    scores: list[dict[str, Any]]
    cards: dict[int, ScoreCard]


def score_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    symbols: Sequence[str], horizon: str, day: date, *, extra_ids: Sequence[int] = (),
    named: tuple[Sequence[int], str] | None = None,
) -> ScoreReport:  # fmt: skip
    """Composite 0-100 score by factor, band and cap, ranked within market and sector. `named`
    is a (ids, basis) universe from `universe_service`, scored instead of the stored bars."""
    if horizon not in HORIZONS:
        raise NiveshError(f"--horizon must be one of {', '.join(HORIZONS)}")
    ids, basis = (list(named[0]), named[1]) if named else universe_ids(duck, sql, symbols)
    ids = sorted({*ids, *extra_ids})
    cfg = settings.analysis
    universe = load_screen_inputs(duck, sql, ids, day, cfg, needs=_SCORE_NEEDS)
    raw, meta, flags = raw_inputs(universe, as_of=day, cfg=cfg)
    cards = score_universe(raw, meta, flags, horizon=horizon, cfg=cfg)  # type: ignore[arg-type]
    scored = [{"symbol": universe[i].symbol, "card": cards[i]} for i in ranking(cards)]
    return ScoreReport(horizon, basis, scored, dict(cards))


@dataclass(frozen=True)
class OneScore:
    security_id: int
    symbol: str
    card: ScoreCard
    universe_size: int
    rank: int | None
    note: str


def score_one(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, query: str,
    horizon: str, day: date,
) -> OneScore:  # fmt: skip
    """The score card of one security, ranked inside the whole stored universe. A cohort of one
    is reported as such (the engine then withholds a top band)."""
    sec = resolve_security(sql, query)
    rep = score_report(duck, sql, settings, [], horizon, day, extra_ids=[sec.id])
    ranked = ranking(rep.cards)
    size = len(rep.cards)
    note = "cohort of one: percentile factors are not meaningful" if size == 1 else ""
    rank = ranked.index(sec.id) + 1 if sec.id in ranked else None
    return OneScore(sec.id, sec.symbol, rep.cards[sec.id], size, rank, note)


# ---- mutual funds -------------------------------------------------------------------------------
@dataclass(frozen=True)
class FundReturns:
    inputs: FundInputs
    analytics: FundAnalytics
    valuation: ValuationReport


def mf_returns_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, code: str,
    benchmark: str | None = None,
) -> FundReturns:  # fmt: skip
    """Rolling returns, consistency vs the benchmark and risk, plus stored-holdings valuation."""
    inp = load_fund_inputs(duck, sql, settings, code, benchmark=benchmark)
    cfg = settings.mf
    res = analyse_fund(
        inp.nav, inp.benchmark, windows=cfg.consistency.window_days, mar_pct=cfg.mar_pct,
        min_alignment_pct=cfg.min_alignment_pct, option=inp.option,
    )  # fmt: skip
    return FundReturns(inp, res, fund_valuation(duck, settings, inp.security_id))


def doctor_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, code: str,
    today: date,
) -> FundReport | None:  # fmt: skip
    """The fund-doctor verdict for one held scheme, or None when it is not held."""
    rep = run_doctor(duck, sql, settings, today=today, only=code)
    return next((f for f in rep.funds if f.fund.amfi_code == code), None)


def fund_meta(duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, code: str) -> FundMeta:
    row = SecurityMaster(sql).by_amfi_code(code.strip())
    if row is None:
        raise NiveshError(f"unknown AMFI code {code!r}; run `nivesh master build` first")
    meta = latest_fund_meta(duck, row.id)
    if meta is None:
        raise NiveshError(f"no stored metadata for scheme {code!r}")
    return meta


@dataclass
class OverlapReport:
    book: Portfolio
    matrix: OverlapMatrix
    look_through: LookThrough
    held_values: dict[str, Decimal | None] = field(default_factory=dict)


def overlap_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings
) -> OverlapReport:  # fmt: skip
    """Pairwise overlap of owned funds and look-through exposure by stock and sector."""
    book = load_portfolio(sql, settings)
    master = SecurityMaster(sql)
    held, rows = {}, {}
    for f in book.funds:
        months = months_stored(duck, f.nav_security_id)
        if months:
            rows[f.amfi_code] = get_fund_holdings(duck, f.nav_security_id, months[-1])
        held[f.amfi_code] = f
    isins = {r.isin for lines in rows.values() for r in lines if r.kind == "equity"}
    isins |= {d.isin for d in book.direct}
    sectors = {i: (master.by_isin(i)[0].sector if master.by_isin(i) else None) for i in isins}
    if book.total <= 0:
        raise NiveshError("no valued holdings; run `nivesh ingest` or `nivesh sync` first")
    owned = [OwnedFund(f.amfi_code, f.name or f.amfi_code, f.value_inr) for f in book.funds]
    usable = {c: r for c, r in rows.items() if held[c].value_inr is not None}
    matrix = overlap_matrix(usable)
    lt = look_through(owned, rows, sectors, book.direct, book.total)
    return OverlapReport(book, matrix, lt, {c: h.value_inr for c, h in held.items()})


# ---- committee facts ----------------------------------------------------------------------------
def _matches_exclusion(sec: SecurityRow, profile: Profile) -> bool:
    """One helper for the risk veto and the universe filter: symbol, ISIN, name or sector."""
    return matches_exclusion(profile.exclusions, sec.symbol, sec.isin, sec.name, sec.sector)


def risk_facts(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, query: str, day: date, *, starter_weight_pct: Decimal,
) -> RiskFacts:  # fmt: skip
    """The facts the risk veto is computed from, assembled in code (never by a model): the
    candidate tested at min(starter weight, the profile's maximum position)."""
    sec = resolve_security(sql, query)
    max_pos = Decimal(str(profile.max_position_pct))
    max_sec = Decimal(str(profile.max_sector_pct))
    weight = min(starter_weight_pct, max_pos)
    excluded = _matches_exclusion(sec, profile)
    hard = False
    if sec.asset_class == "mf":  # fund path: exclusions and the position limit only
        return RiskFacts(excluded, False, False, False, None, max_pos, max_sec, None, weight)
    flag_in = load_flag_inputs(duck, sql, sec.id, day, settings.analysis)
    if flag_in is not None:
        hard = any_hard(detect_flags(flag_in, as_of=day, cfg=settings.analysis))
    inp = load_risk_inputs(
        duck, sql, settings, day, candidate=f"id:{sec.id}", proposed_weight_pct=weight
    )
    res = risk_metrics(
        inp.holdings, inp.candidate, inp.bars, inp.benchmarks, cfg=settings.analysis.risk,
        as_of=day, max_position_pct=max_pos, max_sector_pct=max_sec,
    )  # fmt: skip
    pf = res.pro_forma
    cand_name = inp.candidate.name if inp.candidate else ""
    pos_over = pf is not None and any(w.name == cand_name for w in pf.positions_over_limit)
    sec_name = sec.sector or ""
    sec_over = pf is not None and bool(sec_name) and any(
        w.name == sec_name for w in pf.sectors_over_limit
    )  # fmt: skip
    total = sum((h.value_inr for h in inp.holdings), Decimal(0))
    headroom: Decimal | None = None
    if sec.sector and total > 0:
        held = sum((h.value_inr for h in inp.holdings if h.sector == sec.sector), Decimal(0))
        headroom = max(Decimal(0), max_sec - held / total * 100)
    dtt = res.candidate_days_to_trade.value if res.candidate_days_to_trade else None
    return RiskFacts(
        excluded=excluded, hard_flag=hard, position_over_limit=pos_over,
        sector_over_limit=sec_over, days_to_trade=dtt, max_position_pct=max_pos,
        max_sector_pct=max_sec, sector_headroom_pct=headroom, tested_weight_pct=weight,
    )  # fmt: skip
