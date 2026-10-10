"""Fund doctor orchestration (ST-5.6): read the stores, build `FundFacts`, call the pure engine.

Everything here is read-only and deterministic for a given store state and `today`. A fund whose
data is missing gets facts with the metric None and a reason, so the doctor lists it as
unavailable instead of guessing.
"""

import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import duckdb

from nivesh_adapters.mf_ingest import (
    FundInputs,
    HeldFund,
    Portfolio,
    load_fund_inputs,
    load_portfolio,
    scheme_universe,
)
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.holdings_store import list_transactions
from nivesh_core.market_store import get_bars, get_statement_rows
from nivesh_core.mf_models import FundHoldingRow
from nivesh_core.mf_store import get_fund_holdings, get_nav, latest_fund_meta, months_stored
from nivesh_core.security_master import SecurityMaster
from nivesh_engine.fund_doctor import FundFacts, Position, Verdict, diagnose, tenure_years
from nivesh_engine.fund_screen import Candidate, Constraints, ScreenResult, screen
from nivesh_engine.mf_cost import SchemeInfo, plan_of, resolve_direct_twin, ter_cost
from nivesh_engine.mf_lots import fifo_lots, valued_lots
from nivesh_engine.mf_overlap import pairwise_overlap
from nivesh_engine.mf_returns import FundAnalytics, analyse_fund
from nivesh_engine.mf_valuation import (
    HistoryCompare,
    MultipleResult,
    history_compare,
    stock_multiples,
    weighted_multiple,
)
from nivesh_engine.statements import StatementRow

MAX_PRICE_AGE_DAYS = 10  # a stock price further than this from the month end is not used
HISTORY_MONTHS = 36
HUNDRED = Decimal(100)
CENT = Decimal("0.01")


def trailing_1y_pct(nav: list[tuple[date, Decimal]]) -> Decimal | None:
    """Display-only one-year return in percent; no rule in the doctor reads it (BR-12)."""
    if len(nav) < 2:
        return None
    last_day, last = nav[-1]
    earlier = [v for d, v in nav if d <= last_day - timedelta(days=365)]
    if not earlier:
        return None
    return (HUNDRED * (last / earlier[-1] - 1)).quantize(CENT)


@dataclass
class FundReport:
    fund: HeldFund
    facts: FundFacts
    verdict: Verdict


@dataclass
class DoctorReport:
    funds: list[FundReport] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def scheme_isins(sql: sqlite3.Connection, amfi_code: str) -> set[str]:
    rows = sql.execute(
        "SELECT isin FROM security WHERE amfi_code = ? AND isin IS NOT NULL", (amfi_code,)
    ).fetchall()
    return {r[0] for r in rows}


def _analytics(inp: FundInputs, settings: Settings) -> FundAnalytics:
    cfg = settings.mf
    return analyse_fund(
        inp.nav, inp.benchmark, windows=cfg.consistency.window_days, mar_pct=cfg.mar_pct,
        min_alignment_pct=cfg.min_alignment_pct, option=inp.option,
    )  # fmt: skip


def _consistency_facts(
    res: FundAnalytics | None, why: str
) -> tuple[dict[str, Decimal | None], dict[str, str], bool]:
    """(values, unavailable reasons, comparable) from the analytics; the first configured window
    with benchmark-relative values supplies beat_pct and median_excess."""
    values: dict[str, Decimal | None] = dict.fromkeys(
        ("beat_pct", "median_excess_pct", "downside_capture_pct", "max_drawdown_pct", "sortino")
    )
    gone: dict[str, str] = {}
    if res is None:
        return values, {k: why for k in values}, True
    if not res.comparable:
        return values, {"comparable": res.reason or "not comparable"}, False
    window = next((w for w in res.rolling if w.beat_pct is not None), None)
    if window is not None:
        values["beat_pct"], values["median_excess_pct"] = window.beat_pct, window.median_excess_pct
    elif res.rolling:
        gone["beat_pct"] = gone["median_excess_pct"] = (
            res.rolling[0].relative_reason or "unavailable"
        )
    values["downside_capture_pct"] = res.downside_capture_pct
    if res.downside_capture_pct is None:
        gone["downside_capture_pct"] = res.downside_capture_reason or "unavailable"
    values["sortino"] = res.sortino
    if res.max_drawdown is not None:
        values["max_drawdown_pct"] = res.max_drawdown.pct
    else:
        gone["max_drawdown_pct"] = res.max_drawdown_reason or "unavailable"
    return values, gone, True


def _overlap_with_owned(
    code: str,
    category: str | None,
    holdings: dict[str, list[FundHoldingRow]],
    categories: dict[str, str | None],
) -> tuple[Decimal | None, str | None]:
    """Largest overlap with another owned fund of the same category (None when there is none)."""
    if not category or code not in holdings:
        return None, None
    best: tuple[Decimal, str] | None = None
    for other in sorted(holdings):
        if other == code or categories.get(other) != category:
            continue
        pct = pairwise_overlap(holdings[code], holdings[other]).overlap_pct
        if best is None or pct > best[0]:
            best = (pct, other)
    return (best[0], best[1]) if best else (None, None)


def run_doctor(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    *,
    today: date,
    only: str | None = None,
) -> DoctorReport:
    """A verdict for every held mutual fund (or one AMFI code)."""
    book: Portfolio = load_portfolio(sql, settings)
    out = DoctorReport(skipped=list(book.skipped))
    universe = scheme_universe(sql)
    master = SecurityMaster(sql)
    metas = {f.amfi_code: latest_fund_meta(duck, f.nav_security_id) for f in book.funds}
    categories = {c: (m.category if m else None) for c, m in metas.items()}
    holdings: dict[str, list[FundHoldingRow]] = {}
    for f in book.funds:
        months = months_stored(duck, f.nav_security_id)
        if months and f.value_inr is not None:
            holdings[f.amfi_code] = get_fund_holdings(duck, f.nav_security_id, months[-1])
    all_txns = list_transactions(sql)
    has_candidates: dict[str | None, bool] = {}
    for fund in book.funds:
        if only is not None and fund.amfi_code != only:
            continue
        meta = metas[fund.amfi_code]
        name = fund.name or fund.amfi_code
        try:
            inp: FundInputs | None = load_fund_inputs(duck, sql, settings, fund.amfi_code)
            why = ""
        except NiveshError as e:
            inp, why = None, str(e)
        res = _analytics(inp, settings) if inp else None
        values, gone, comparable = _consistency_facts(res, why)
        if inp is not None and inp.benchmark_reason:  # say why, not just "series missing"
            gone = {k: inp.benchmark_reason if v == "benchmark series missing" else v
                    for k, v in gone.items()}  # fmt: skip
        plan = meta.plan if meta else plan_of(name)
        twin = cost = None
        direct_ter = None
        if plan == "regular":
            twin = resolve_direct_twin(
                SchemeInfo(fund.amfi_code, name, meta.amc if meta else None), universe
            )
            twin_row = master.by_amfi_code(twin.amfi_code) if twin.amfi_code else None
            twin_meta = latest_fund_meta(duck, twin_row.id) if twin_row else None
            direct_ter = twin_meta.expense_ratio if twin_meta else None
            cost = ter_cost(
                meta.expense_ratio if meta else None, direct_ter, fund.value_inr, twin=twin
            )
            if cost.ter_gap_pct is None:
                gone["ter_gap_pct"] = cost.reason or "unavailable"
        overlap, overlap_with = _overlap_with_owned(
            fund.amfi_code, categories.get(fund.amfi_code), holdings, categories
        )
        since = meta.manager_since if meta else None
        tenure = tenure_years(since, today)
        if tenure is None:
            gone["tenure_years"] = "manager start date unknown"
        valuation = fund_valuation(duck, settings, fund.nav_security_id)
        if valuation.ratio is None:
            gone["valuation_ratio"] = valuation.why_no_ratio()
        facts = FundFacts(
            fund.amfi_code, name, plan, twin, cost.ter_gap_pct if cost else None, overlap,
            overlap_with, comparable, values["beat_pct"], values["median_excess_pct"],
            values["downside_capture_pct"], values["max_drawdown_pct"], values["sortino"], tenure,
            valuation.ratio, trailing_1y_pct(inp.nav) if inp else None, gone,
        )  # fmt: skip
        isins = scheme_isins(sql, fund.amfi_code)
        txns = [t for t in all_txns if t.amfi_code == fund.amfi_code or t.isin in isins]
        latest = inp.nav[-1] if inp else None
        lots = valued_lots(
            fifo_lots(txns), latest[1] if latest else None, latest[0] if latest else None, today,
            settings.mf.max_nav_age_days, fund.quantity,
        )  # fmt: skip
        position = Position(lots, meta.category if meta else None)
        category = categories[fund.amfi_code]
        if category not in has_candidates:  # a screened candidate: what rule 3's REPLACE needs
            found = discover(duck, sql, settings, category, Constraints()) if category else None
            has_candidates[category] = bool(found and found.result.ranked)
        verdict = diagnose(facts, settings.mf, has_candidates[category], position)
        out.funds.append(FundReport(fund, facts, verdict))
    return out


@dataclass
class ValuationReport:
    """Weighted P/E and P/B of a fund's holdings against the fund's own month-end history."""

    month_end: date | None = None
    pe: MultipleResult | None = None
    pb: MultipleResult | None = None
    pe_history: HistoryCompare | None = None
    pb_history: HistoryCompare | None = None
    stocks_with_data: int = 0
    stocks_total: int = 0
    reason: str | None = None

    @property
    def ratio(self) -> Decimal | None:
        """Current multiple over its own median: P/E, else P/B (what the doctor's rule reads)."""
        for h in (self.pe_history, self.pb_history):
            if h is not None and h.ratio is not None:
                return h.ratio
        return None

    def why_no_ratio(self) -> str:
        if self.reason:
            return self.reason
        notes = [h.reason for h in (self.pe_history, self.pb_history) if h and h.reason]
        return "; ".join(notes) or "valuation unavailable"


def _price_on(bars: list[tuple[date, Decimal]], day: date) -> Decimal | None:
    i = bisect_right([d for d, _ in bars], day) - 1
    if i < 0 or (day - bars[i][0]).days > MAX_PRICE_AGE_DAYS:
        return None
    return bars[i][1]


def fund_valuation(
    duck: duckdb.DuckDBPyConnection, settings: Settings, security_id: int
) -> ValuationReport:
    """Per-stock multiples from stored statements and bars as of each stored month end (no
    look-ahead), weighted by the fund's holdings; history is the latest 36 prior months."""
    months = months_stored(duck, security_id)
    if not months:
        return ValuationReport(reason="no stored holdings; run `nivesh mf holdings`")
    cfg = settings.mf
    bars: dict[int, list[tuple[date, Decimal]]] = {}
    statements: dict[int, list[StatementRow]] = {}
    pe_by_month: dict[date, Decimal | None] = {}
    pb_by_month: dict[date, Decimal | None] = {}
    results: list[tuple[MultipleResult, MultipleResult, int, int]] = []
    for month in months[-(HISTORY_MONTHS + 1) :]:
        rows = get_fund_holdings(duck, security_id, month)
        pes: dict[str, Decimal | None] = {}
        pbs: dict[str, Decimal | None] = {}
        for r in rows:
            sid = r.holding_security_id
            if r.kind != "equity" or sid is None:
                continue
            if sid not in bars:
                bars[sid] = [(b.date, b.close) for b in get_bars(duck, sid)]
                statements[sid] = get_statement_rows(duck, sid)
            pes[r.isin], pbs[r.isin] = stock_multiples(
                statements[sid], _price_on(bars[sid], month), month
            )
        pe = weighted_multiple(rows, pes, cfg.min_valuation_coverage_pct)
        pb = weighted_multiple(rows, pbs, cfg.min_valuation_coverage_pct)
        pe_by_month[month], pb_by_month[month] = pe.value, pb.value
        with_data = sum(1 for i in pes if pes[i] is not None or pbs[i] is not None)
        results.append((pe, pb, with_data, sum(1 for r in rows if r.kind == "equity")))
    last = results[-1]
    current = months[-1]
    pe_hist = {m: v for m, v in pe_by_month.items() if m != current}
    pb_hist = {m: v for m, v in pb_by_month.items() if m != current}
    ratio = cfg.thresholds.valuation_stretch_ratio
    return ValuationReport(
        current, last[0], last[1],
        history_compare(pe_hist, last[0].value, ratio),
        history_compare(pb_hist, last[1].value, ratio),
        last[2], last[3],
    )  # fmt: skip


@dataclass
class DiscoverReport:
    category: str
    result: ScreenResult
    considered: int
    not_loaded: list[str] = field(default_factory=list)


def discover(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    category: str,
    constraints: Constraints,
) -> DiscoverReport:
    """Screen the stored schemes of a category (the owner's loaded universe, not the market)."""
    cfg = settings.mf
    master = SecurityMaster(sql)
    book = load_portfolio(sql, settings)
    owned: dict[str, list[FundHoldingRow]] = {}
    for f in book.funds:
        months = months_stored(duck, f.nav_security_id)
        if months:
            owned[f.amfi_code] = get_fund_holdings(duck, f.nav_security_id, months[-1])
    owned_codes = {f.amfi_code for f in book.funds}
    cands: list[Candidate] = []
    not_loaded: list[str] = []
    stored = dict(master.mf_schemes())
    for code in sorted(set(cfg.screen.universe) | set(stored)):
        row = stored.get(code)
        meta = latest_fund_meta(duck, row.id) if row else None
        in_category = meta is not None and (meta.category or "").casefold() == category.casefold()
        if code in cfg.screen.universe and not (row and meta and get_nav(duck, row.id)):
            not_loaded.append(f"{code}: run `nivesh mf nav`, `meta` and `holdings` first")
            continue
        if row is None or meta is None or not in_category:
            continue
        try:
            inp = load_fund_inputs(duck, sql, settings, code)
        except NiveshError:
            not_loaded.append(f"{code}: no stored NAV; run `nivesh mf nav {code}`")
            continue
        res = _analytics(inp, settings)
        values, _gone, comparable = _consistency_facts(res, "")
        months = months_stored(duck, row.id)
        held = get_fund_holdings(duck, row.id, months[-1]) if months else []
        cands.append(
            Candidate(
                code,
                meta.scheme_name,
                meta.plan,
                meta.option,
                comparable,
                meta.aum_crore,
                meta.expense_ratio,
                tenure_years(meta.manager_since, _last_nav_day(inp)),
                values["beat_pct"],
                values["median_excess_pct"],
                values["downside_capture_pct"],
                fund_valuation(duck, settings, row.id).ratio,
                held,
            )  # fmt: skip
        )
    owned_stored = {c: r for c, r in owned.items() if c in owned_codes}
    result = screen(cands, constraints, owned_stored, cfg.screen.weights)
    return DiscoverReport(category, result, len(cands), not_loaded)


def _last_nav_day(inp: FundInputs) -> date:
    return inp.nav[-1][0]
