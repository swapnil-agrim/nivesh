"""Valuation multiples with their own history, peer median, reverse DCF and fair-value range
(ST-6.4). Pure and Decimal-only; the loader assembles the inputs.

Multiples: P/E (price over trailing-four-quarter EPS, else the last annual EPS, labelled), P/B,
EV/EBITDA, free-cash-flow yield and dividend yield. Prices are raw closes, on the same share basis
as the statements filed at the time, so a split never distorts the history. Each month-end
observation uses only statements filed on or before that month end. A multiple that is not
meaningful (negative EPS, a financial company's EBITDA, a missing share count) is an unavailable
`Metric` with a reason and is left out of the history. The percentile is the share of the window's
observations at or below the current value; a window needs `min_obs` observations and a history
as long as the window, otherwise it says how many observations or years there are.

The DCF values a cash flow (free cash flow, or earnings for financials) grown at a constant rate
for the horizon, plus a terminal value. The reverse DCF solves the growth rate that equates it to
the market capitalisation, between -50% and +100%, at the base scenario's discount rate and
terminal growth. The fair-value range runs the base, bull and bear scenarios of the owner's config;
every assumption is echoed in the result. Nothing here is a recommendation.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext

from nivesh_core.analysis_config import AnalysisSettings, DcfScenario
from nivesh_engine import dmath
from nivesh_engine.metric import Metric, na, ok
from nivesh_engine.statements import StatementRow, latest_as_of

QUANTUM = Decimal("0.00000001")
MULTIPLES = ("pe", "pb", "ev_ebitda", "fcf_yield_pct", "dividend_yield_pct")
SPAN_SLACK_DAYS = 62
GROWTH_LOW, GROWTH_HIGH = Decimal("-0.5"), Decimal(1)
SOLVER_STEPS = 80
_FINANCIAL_NOTE = "not meaningful for financials (use P/B and P/E)"


@dataclass(frozen=True)
class ValuationInputs:
    """Everything a valuation needs: every filed statement row, raw month-end closes (oldest
    first, up to the as-of date), and the latest close on or before it."""

    market: str
    sector_kind: str
    as_of: date
    rows: tuple[StatementRow, ...]
    month_ends: tuple[tuple[date, Decimal], ...]
    last_close: tuple[date, Decimal] | None


@dataclass(frozen=True)
class PeerMultiples:
    """Current multiples of the peers that have one, by multiple name."""

    values: Mapping[str, tuple[Decimal, ...]]
    considered: int = 0
    reason: str | None = None
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultipleResult:
    name: str
    current: Metric
    medians: Mapping[str, Metric]  # by window, "5y" and "10y"
    percentiles: Mapping[str, Metric]
    peer_median: Metric


@dataclass(frozen=True)
class ValuationResult:
    as_of: date
    market: str
    sector_kind: str
    price: Decimal | None
    price_date: date | None
    market_cap: Decimal | None
    multiples: dict[str, MultipleResult]


@dataclass(frozen=True)
class Scenario:
    name: str
    growth_pct: Decimal
    discount_pct: Decimal
    terminal_growth_pct: Decimal
    equity_value: Metric
    per_share: Metric


@dataclass(frozen=True)
class RangeResult:
    as_of: date
    cash_flow: Metric
    implied_growth: Metric
    scenarios: dict[str, Scenario]
    assumptions: dict[str, object]


def _q(value: Decimal) -> Decimal:
    return dmath.quantize(value, QUANTUM)


@dataclass(frozen=True)
class _Facts:
    eps: Decimal | None
    eps_basis: str | None
    shares: Decimal | None
    equity: Decimal | None
    debt: Decimal | None
    cash: Decimal | None
    operating_income: Decimal | None
    depreciation: Decimal | None
    cfo: Decimal | None
    capex: Decimal | None
    dividends: Decimal | None


def _facts(rows: Sequence[StatementRow], d: date) -> _Facts:
    seen = latest_as_of(rows, d)  # newest period first, nothing filed after d
    annual: dict[str, Decimal] = {}
    newest: dict[str, Decimal] = {}
    quarters: list[tuple[date, Decimal]] = []
    for r in seen:
        if r.period_end > d:
            continue
        newest.setdefault(r.item, r.value)
        if r.period_type == "A":
            annual.setdefault(r.item, r.value)
        elif r.item == "eps":
            quarters.append((r.period_end, r.value))
    eps, basis = None, None
    if len(quarters) >= 4 and 240 <= (quarters[0][0] - quarters[3][0]).days <= 300:
        eps, basis = sum((v for _, v in quarters[:4]), Decimal(0)), "ttm"
    elif "eps" in annual:
        eps, basis = annual["eps"], "last_annual"
    capex = annual.get("capex")
    return _Facts(
        eps, basis, newest.get("shares_out"), newest.get("total_equity"),
        newest.get("total_debt"), newest.get("cash"), annual.get("operating_income"),
        annual.get("depreciation_amortization"), annual.get("cfo"),
        None if capex is None else abs(capex), annual.get("dividends_per_share"),
    )  # fmt: skip


def _market_cap(f: _Facts, price: Decimal) -> Decimal | None:
    return None if f.shares is None else price * f.shares


def _no_cap() -> str:
    return "no shares_out filed, so the market capitalisation is unknown"


def _multiples_at(f: _Facts, price: Decimal, kind: str, market: str, d: date) -> dict[str, Metric]:
    cap = _market_cap(f, price)
    out: dict[str, Metric] = {}
    if f.eps is None:
        out["pe"] = na(f"no EPS filed on or before {d}", price=price)
    elif f.eps <= 0:
        out["pe"] = na(f"non-positive EPS ({f.eps}): P/E is not meaningful", price=price,
                       eps=f.eps, eps_basis=f.eps_basis)  # fmt: skip
    else:
        out["pe"] = ok(_q(price / f.eps), price=price, eps=f.eps, eps_basis=f.eps_basis)
    if cap is None:
        out["pb"] = na(_no_cap(), price=price)
    elif f.equity is None:
        out["pb"] = na("no total_equity filed", market_cap=cap)
    elif f.equity <= 0:
        out["pb"] = na(f"non-positive equity ({f.equity}): P/B is not meaningful", market_cap=cap)
    else:
        out["pb"] = ok(_q(cap / f.equity), market_cap=cap, equity=f.equity)
    out["ev_ebitda"] = _ev_ebitda(f, cap, kind, market)
    out["fcf_yield_pct"] = _fcf_yield(f, cap, kind)
    if f.dividends is None:
        out["dividend_yield_pct"] = na("no dividends_per_share filed", price=price)
    else:
        out["dividend_yield_pct"] = ok(
            _q(f.dividends / price * dmath.HUNDRED), dividends_per_share=f.dividends, price=price
        )
    return out


def _ev_ebitda(f: _Facts, cap: Decimal | None, kind: str, market: str) -> Metric:
    if kind == "financial":
        return na(_FINANCIAL_NOTE)
    if cap is None:
        return na(_no_cap())
    if market == "IN":
        return na("India operating_income is profit before tax, not EBIT; EBITDA is unavailable")
    missing = [
        n for n, v in (("operating_income", f.operating_income),
                       ("depreciation_amortization", f.depreciation),
                       ("total_debt", f.debt), ("cash", f.cash)) if v is None
    ]  # fmt: skip
    if (
        missing
        or f.operating_income is None
        or f.depreciation is None
        or f.debt is None
        or f.cash is None
    ):
        return na(f"no {', '.join(missing)} filed", market_cap=cap)
    ebitda = f.operating_income + f.depreciation
    if ebitda <= 0:
        return na(f"non-positive EBITDA ({ebitda}): EV/EBITDA is not meaningful", market_cap=cap)
    ev = cap + f.debt - f.cash
    return ok(_q(ev / ebitda), enterprise_value=ev, ebitda=ebitda)


def _fcf_yield(f: _Facts, cap: Decimal | None, kind: str) -> Metric:
    if kind == "financial":
        return na(_FINANCIAL_NOTE)
    if cap is None:
        return na(_no_cap())
    if f.cfo is None or f.capex is None:
        return na("no cfo and capex filed", market_cap=cap)
    fcf = f.cfo - f.capex
    return ok(_q(fcf / cap * dmath.HUNDRED), free_cash_flow=fcf, market_cap=cap)


def _no_close(inputs: ValuationInputs) -> dict[str, Metric]:
    return {m: na(f"no close on or before {inputs.as_of}") for m in MULTIPLES}


def current_multiples(inputs: ValuationInputs) -> dict[str, Metric]:
    """The five multiples at the latest close on or before the as-of date."""
    if inputs.last_close is None:
        return _no_close(inputs)
    d, price = inputs.last_close
    with localcontext(dmath.CONTEXT):
        f = _facts(inputs.rows, inputs.as_of)
        return _multiples_at(f, price, inputs.sector_kind, inputs.market, d)


def _window_start(as_of: date, years: int) -> date:
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:  # 29 February
        return as_of.replace(year=as_of.year - years, day=28)


def _window_stats(
    obs: list[tuple[date, Decimal]], current: Metric, years: int, cfg: AnalysisSettings, as_of: date
) -> tuple[Metric, Metric]:
    n = len(obs)
    need = cfg.valuation.min_obs
    why: str | None = None
    if n < need:
        why = f"{n} observations in the {years}y window, need {need}"
    else:
        span = Decimal((as_of - obs[0][0]).days) / Decimal("365.25")
        if obs[0][0] > _window_start(as_of, years) + timedelta(days=SPAN_SLACK_DAYS):
            why = (
                f"history spans {dmath.quantize(span, Decimal('0.1'))} years "
                f"(first observation {obs[0][0]}), need {years}"
            )
    if why:
        return na(why, observations=n), na(why, observations=n)
    values = [v for _, v in obs]
    median = ok(_q(dmath.median(values)), observations=n, first=obs[0][0], years=years)
    if not current.available or current.value is None:
        return median, na("the current value is unavailable", observations=n)
    at_or_below = sum(1 for v in values if v <= current.value)
    pct = _q(dmath.HUNDRED * at_or_below / n)
    return median, ok(pct, observations=n, years=years)


def _peer_median(name: str, peers: PeerMultiples | None, cfg: AnalysisSettings) -> Metric:
    if peers is None:
        return na("no peer set supplied")
    got = peers.values.get(name, ())
    if peers.considered == 0 and not got:
        return na(peers.reason or "no peers found")
    need = cfg.valuation.min_peers
    if len(got) < need:
        return na(
            f"{len(got)} peers with a value, need {need}", peers=len(got),
            considered=peers.considered,
        )  # fmt: skip
    return ok(_q(dmath.median(list(got))), peers=len(got), considered=peers.considered)


def valuation_multiples(
    inputs: ValuationInputs, peers: PeerMultiples | None, *, cfg: AnalysisSettings
) -> ValuationResult:
    """Current multiples with 5y and 10y medians and percentiles, and the peer median."""
    cur = current_multiples(inputs)
    price = inputs.last_close
    with localcontext(dmath.CONTEXT):
        history: dict[str, list[tuple[date, Decimal]]] = {m: [] for m in MULTIPLES}
        for d, close in inputs.month_ends:
            if d > inputs.as_of:
                continue
            at = _multiples_at(_facts(inputs.rows, d), close, inputs.sector_kind, inputs.market, d)
            for name, m in at.items():
                if m.available and m.value is not None:
                    history[name].append((d, m.value))
        out: dict[str, MultipleResult] = {}
        for name in MULTIPLES:
            medians: dict[str, Metric] = {}
            ranks: dict[str, Metric] = {}
            for years in cfg.valuation.history_years:
                start = _window_start(inputs.as_of, years)
                window = [(d, v) for d, v in history[name] if d > start]
                medians[f"{years}y"], ranks[f"{years}y"] = _window_stats(
                    window, cur[name], years, cfg, inputs.as_of
                )
            out[name] = MultipleResult(
                name, cur[name], medians, ranks, _peer_median(name, peers, cfg)
            )
        facts = _facts(inputs.rows, inputs.as_of)
        cap = None if price is None else _market_cap(facts, price[1])
    return ValuationResult(
        inputs.as_of, inputs.market, inputs.sector_kind,
        None if price is None else price[1], None if price is None else price[0], cap, out,
    )  # fmt: skip


# ---- DCF ----------------------------------------------------------------------------------------
def dcf_value(
    cash_flow: Decimal, growth: Decimal, discount: Decimal, terminal: Decimal, years: int
) -> Decimal:
    """Present value of `cash_flow` grown at `growth` a year for `years` years (the first flow is
    one year out) plus a terminal value at `terminal` growth. Rates are fractions."""
    with localcontext(dmath.CONTEXT):
        flow, factor, total = cash_flow, dmath.ONE, dmath.ZERO
        for _ in range(years):
            flow *= 1 + growth
            factor *= 1 + discount
            total += flow / factor
        return total + flow * (1 + terminal) / (discount - terminal) / factor


def _solve_growth(
    cap: Decimal, cash_flow: Decimal, sc: DcfScenario, years: int
) -> tuple[Decimal | None, str | None]:
    disc, term = sc.discount_pct / dmath.HUNDRED, sc.terminal_growth_pct / dmath.HUNDRED

    def value(g: Decimal) -> Decimal:
        return dcf_value(cash_flow, g, disc, term, years)

    low_v, high_v = value(GROWTH_LOW), value(GROWTH_HIGH)
    if cap < low_v:
        return None, f"market cap {cap} is below the DCF value {_q(low_v)} even at -50% growth"
    if cap > high_v:
        return None, f"market cap {cap} exceeds the DCF value {_q(high_v)} even at +100% growth"
    lo, hi = GROWTH_LOW, GROWTH_HIGH
    with localcontext(dmath.CONTEXT):
        for _ in range(SOLVER_STEPS):
            mid = (lo + hi) / 2
            if value(mid) < cap:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2, None


def _cash_flow(inputs: ValuationInputs) -> Metric:
    seen = latest_as_of(inputs.rows, inputs.as_of)
    annual = {(r.item, r.period_end): r.value for r in seen if r.period_type == "A"}
    if inputs.sector_kind == "financial":
        ends = sorted((e for i, e in annual if i == "net_income"), reverse=True)
        if not ends:
            return na("no annual net_income filed (earnings are the cash flow for financials)")
        return ok(annual[("net_income", ends[0])], basis="earnings", period_end=ends[0])
    ends = sorted((e for i, e in annual if i == "cfo" and ("capex", e) in annual), reverse=True)
    if not ends:
        return na("no annual cfo and capex filed for the same year (free cash flow)")
    cfo, capex = annual[("cfo", ends[0])], abs(annual[("capex", ends[0])])
    return ok(cfo - capex, basis="fcf", period_end=ends[0], cfo=cfo, capex=capex)


def valuation_range(inputs: ValuationInputs, *, cfg: AnalysisSettings) -> RangeResult:
    """Reverse-DCF implied growth and the base, bull and bear fair-value range."""
    dcf = cfg.valuation.dcf
    scenarios_cfg = {"bear": dcf.bear, "base": dcf.base, "bull": dcf.bull}
    cf = _cash_flow(inputs)
    facts = _facts(inputs.rows, inputs.as_of)
    price = inputs.last_close
    cap = None if price is None or facts.shares is None else price[1] * facts.shares
    positive = cf.available and cf.value is not None and cf.value > 0
    if cf.available and not positive:
        why = f"non-positive cash flow ({cf.value}): a DCF on it is not meaningful"
        cf = na(why, **cf.inputs)
    else:
        why = cf.reason or ""
    scenarios: dict[str, Scenario] = {}
    with localcontext(dmath.CONTEXT):
        for name, sc in scenarios_cfg.items():
            if positive and cf.value is not None:
                equity = dcf_value(
                    cf.value, sc.growth_pct / dmath.HUNDRED, sc.discount_pct / dmath.HUNDRED,
                    sc.terminal_growth_pct / dmath.HUNDRED, dcf.horizon_years,
                )  # fmt: skip
                eq_m = ok(_q(equity), basis=cf.inputs.get("basis"))
                if facts.shares:
                    per = ok(_q(equity / facts.shares), shares_out=facts.shares)
                else:
                    per = na("no shares_out filed: equity value only")
            else:
                eq_m = na(why)
                per = na(why)
            scenarios[name] = Scenario(
                name, sc.growth_pct, sc.discount_pct, sc.terminal_growth_pct, eq_m, per
            )
        implied = _implied(cf, positive, cap, cfg, why)
    assumptions: dict[str, object] = {
        "horizon_years": dcf.horizon_years,
        "cash_flow_basis": cf.inputs.get("basis"),
        "cash_flow": cf.value,
        "scenarios": {
            n: {"growth_pct": s.growth_pct, "discount_pct": s.discount_pct,
                "terminal_growth_pct": s.terminal_growth_pct}
            for n, s in scenarios_cfg.items()
        },
        "reverse_dcf": {"discount_pct": dcf.base.discount_pct,
                        "terminal_growth_pct": dcf.base.terminal_growth_pct,
                        "growth_bounds_pct": [Decimal(-50), Decimal(100)]},
        "shares_out": facts.shares,
        "market_cap": cap,
        "price": None if price is None else price[1],
        "price_date": None if price is None else price[0],
    }  # fmt: skip
    return RangeResult(inputs.as_of, cf, implied, scenarios, assumptions)


def _implied(
    cf: Metric, positive: bool, cap: Decimal | None, cfg: AnalysisSettings, why: str
) -> Metric:
    if not positive or cf.value is None:
        return na(why)
    if cap is None:
        return na("market cap unknown: needs a close and shares_out", cash_flow=cf.value)
    growth, reason = _solve_growth(
        cap, cf.value, cfg.valuation.dcf.base, cfg.valuation.dcf.horizon_years
    )
    if growth is None:
        return na(reason or "no solution", market_cap=cap, cash_flow=cf.value)
    base = cfg.valuation.dcf.base
    return ok(
        _q(growth * dmath.HUNDRED), market_cap=cap, cash_flow=cf.value,
        discount_pct=base.discount_pct, terminal_growth_pct=base.terminal_growth_pct,
        horizon_years=cfg.valuation.dcf.horizon_years,
    )  # fmt: skip
