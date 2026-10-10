"""Portfolio risk metrics (ST-6.7): volatility, drawdown, beta, correlation, liquidity, pro-forma.

Pure and Decimal-only: no I/O, no clock. Returns are simple daily returns in each security's own
currency (INR/USD sensitivity is out of scope), computed on the dates the series share. The
portfolio is the constant-weight portfolio of today's weights (a backtest of today's weights, not a
record of what was held). A holding without usable bars is left out of the volatility, drawdown
and beta figures, listed under `excluded`, and the share of value that remains is reported. A
figure that cannot be computed is an unavailable metric with a reason, never zero. Volatility is a
fraction (0.2 is 20 percent a year), drawdown is in percent.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.analysis_config import RiskSettings
from nivesh_engine import dmath
from nivesh_engine.bars import Bar
from nivesh_engine.metric import Metric, na, ok
from nivesh_engine.universe import adv_value

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
QUANTUM = Decimal("0.00000001")
UNCLASSIFIED = "unclassified"
SESSIONS_PER_YEAR = 252


@dataclass(frozen=True)
class RiskHolding:
    """A held security: its INR value, market (picks the benchmark), sector, and the INR value of
    one unit of its price currency (1 for INR)."""

    security_id: int
    name: str
    value_inr: Decimal
    market: str = "IN"
    sector: str | None = None
    inr_per_unit: Decimal = ONE


@dataclass(frozen=True)
class RiskCandidate:
    """A security under consideration at a proposed weight (percent of the current portfolio)."""

    security_id: int
    name: str
    proposed_weight_pct: Decimal
    market: str = "IN"
    sector: str | None = None
    inr_per_unit: Decimal = ONE


@dataclass(frozen=True)
class HoldingRisk:
    security_id: int
    name: str
    weight_pct: Decimal
    beta: Metric
    correlation: Metric  # of the candidate with this holding
    days_to_trade: Metric


@dataclass(frozen=True)
class ExcludedHolding:
    security_id: int
    name: str
    reason: str


@dataclass(frozen=True)
class Weight:
    name: str
    weight_pct: Decimal


@dataclass(frozen=True)
class ProForma:
    candidate_weight_pct: Decimal
    positions: tuple[Weight, ...]
    sectors: tuple[Weight, ...]
    hhi: Decimal
    effective_positions: Decimal
    positions_over_limit: tuple[Weight, ...] = ()
    sectors_over_limit: tuple[Weight, ...] = ()


@dataclass(frozen=True)
class RiskResult:
    as_of: date | None
    total_inr: Decimal
    coverage_pct: Decimal | None
    excluded: tuple[ExcludedHolding, ...] = ()
    portfolio_vol: Metric = field(default_factory=lambda: na("not computed"))
    max_drawdown_pct: dict[str, Metric] = field(default_factory=dict)
    beta: Metric = field(default_factory=lambda: na("not computed"))
    holdings: tuple[HoldingRisk, ...] = ()
    candidate_days_to_trade: Metric | None = None
    pro_forma: ProForma | None = None


Closes = Mapping[date, Decimal]


def _closes(series: Sequence[Bar], as_of: date | None) -> Closes:
    return {b.date: b.close for b in series if b.close > 0 and (as_of is None or b.date <= as_of)}


def _common(*series: Closes) -> list[date]:
    dates = set(series[0])
    for other in series[1:]:
        dates &= other.keys()
    return sorted(dates)


def _returns(closes: Closes, dates: Sequence[date]) -> list[Decimal]:
    return [closes[b] / closes[a] - ONE for a, b in zip(dates, dates[1:], strict=False)]


def _q(value: Decimal) -> Decimal:
    return dmath.quantize(value, QUANTUM)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    return _q(HUNDRED * part / total)


def _overlap(a: Closes, b: Closes, cfg: RiskSettings) -> tuple[list[date], str | None]:
    """The shared dates of the lookback window, or why there are too few."""
    dates = _common(a, b)[-(cfg.lookback_days + 1) :]
    if len(dates) - 1 < cfg.min_overlap_days:
        return (
            dates,
            f"overlap of {max(len(dates) - 1, 0)} days is below the required "
            f"{cfg.min_overlap_days}",
        )
    return dates, None


def _volatility(
    ids: Sequence[int],
    weights: Mapping[int, Decimal],
    closes: Mapping[int, Closes],
    dates: Sequence[date],
    cfg: RiskSettings,
) -> Metric:
    tail = list(dates[-(cfg.lookback_days + 1) :])
    if len(tail) - 1 < cfg.min_overlap_days:
        why = (
            f"overlap of {max(len(tail) - 1, 0)} days is below the required {cfg.min_overlap_days}"
        )
        return na(why, days=len(tail) - 1)
    rets = {i: _returns(closes[i], tail) for i in ids}
    var = ZERO
    for n, i in enumerate(ids):
        for j in ids[n:]:
            cov = dmath.covariance(rets[i], rets[j]) or ZERO  # None only below two returns
            var += (1 if i == j else 2) * weights[i] * weights[j] * cov
    vol = dmath.sqrt(max(var, ZERO)) * dmath.sqrt(dmath.TRADING_DAYS)
    return ok(_q(vol), days=len(tail) - 1, holdings=len(ids))


def _drawdown(
    years: int,
    ids: Sequence[int],
    weights: Mapping[int, Decimal],
    closes: Mapping[int, Closes],
    dates: Sequence[date],
) -> Metric:
    need = years * SESSIONS_PER_YEAR + 1
    if len(dates) < need:
        return na(f"need {need} aligned bars, have {len(dates)}")
    window = dates[-need:]
    rets = {i: _returns(closes[i], window) for i in ids}
    level = peak = ONE
    worst = ZERO
    for t in range(need - 1):
        level *= ONE + sum((weights[i] * rets[i][t] for i in ids), ZERO)
        peak = max(peak, level)
        worst = max(worst, (peak - level) / peak)
    return ok(_q(HUNDRED * worst), sessions=need - 1)


def _beta(h: RiskHolding, mine: Closes, bench: Closes | None, cfg: RiskSettings) -> Metric:
    if not bench:
        return na(f"no benchmark series for market {h.market}")
    if not mine:
        return na("no bars for this holding")
    dates, why = _overlap(mine, bench, cfg)
    if why:
        return na(why)
    value = dmath.beta(_returns(mine, dates), _returns(bench, dates))
    return (
        na("the benchmark has no variance") if value is None else ok(_q(value), days=len(dates) - 1)
    )


def _correlation(mine: Closes, cand: Closes | None, cfg: RiskSettings) -> Metric:
    if cand is None:
        return na("no candidate given")
    if not cand or not mine:
        return na("no bars for the candidate" if not cand else "no bars for this holding")
    dates, why = _overlap(mine, cand, cfg)
    if why:
        return na(why)
    value = dmath.correlation(_returns(mine, dates), _returns(cand, dates))
    return na("a series has no variance") if value is None else ok(_q(value), days=len(dates) - 1)


def _days_to_trade(
    series: Sequence[Bar],
    size: Decimal,
    inr_per_unit: Decimal,
    as_of: date | None,
    cfg: RiskSettings,
) -> Metric:
    got = adv_value(series, cfg.adv_days, inr_per_unit, as_of)
    if got.value is None:
        return na(got.reason or "no volume")
    adv = got.value
    if adv <= 0:
        return na("average daily value traded is zero")
    daily = cfg.participation_pct / HUNDRED * adv
    return ok(_q(size / daily), adv_inr=_q(adv), participation_pct=cfg.participation_pct)


def _pro_forma(
    held: Sequence[RiskHolding],
    total: Decimal,
    cand: RiskCandidate,
    max_position_pct: Decimal | None,
    max_sector_pct: Decimal | None,
) -> ProForma:
    w = cand.proposed_weight_pct
    positions = [(h.name, h.sector, h.value_inr / total * (HUNDRED - w)) for h in held]
    positions.append((cand.name, cand.sector, w))
    ranked = sorted(positions, key=lambda p: (-p[2], p[0]))
    by_sector: dict[str, Decimal] = {}
    for _, sector, weight in positions:
        by_sector[sector or UNCLASSIFIED] = by_sector.get(sector or UNCLASSIFIED, ZERO) + weight
    sectors = sorted(by_sector.items(), key=lambda kv: (-kv[1], kv[0]))
    hhi = sum(((p[2] / HUNDRED) ** 2 for p in positions), ZERO)
    return ProForma(
        w,
        tuple(Weight(n, _q(x)) for n, _, x in ranked),
        tuple(Weight(n, _q(x)) for n, x in sectors),
        _q(hhi),
        _q(ONE / hhi),
        tuple(
            Weight(n, _q(x))
            for n, _, x in ranked
            if max_position_pct is not None and x > max_position_pct
        ),
        tuple(
            Weight(n, _q(x))
            for n, x in sectors
            if max_sector_pct is not None and n != UNCLASSIFIED and x > max_sector_pct
        ),
    )


def risk_metrics(
    holdings: Sequence[RiskHolding],
    candidate: RiskCandidate | None,
    bars: Mapping[int, Sequence[Bar]],
    benchmarks: Mapping[str, Sequence[Bar]],
    *,
    cfg: RiskSettings,
    as_of: date | None = None,
    max_position_pct: Decimal | None = None,
    max_sector_pct: Decimal | None = None,
) -> RiskResult:
    """Volatility, drawdown, beta, candidate correlation, days to trade and pro-forma weights.

    `bars` is by security id (holdings and the candidate), `benchmarks` by market. Only bars on or
    before `as_of` are used. The candidate's `proposed_weight_pct` (above 0, at most 100) is
    funded pro rata from the current holdings; the limits, when given, flag pro-forma breaches."""
    if candidate is not None and not (0 < candidate.proposed_weight_pct <= HUNDRED):
        raise ValueError("proposed_weight_pct must be above 0 and at most 100")
    with localcontext(dmath.CONTEXT):
        held = sorted(
            (h for h in holdings if h.value_inr > 0), key=lambda h: (-h.value_inr, h.security_id)
        )
        total = sum((h.value_inr for h in held), ZERO)
        closes = {h.security_id: _closes(bars.get(h.security_id, ()), as_of) for h in held}
        need = cfg.min_overlap_days + 1
        usable = [h for h in held if len(closes[h.security_id]) >= need]
        excluded = tuple(
            ExcludedHolding(
                h.security_id,
                h.name,
                "no bars stored"
                if not closes[h.security_id]
                else f"only {len(closes[h.security_id])} bars; need {need}",
            )
            for h in held
            if h not in usable
        )
        ids = [h.security_id for h in usable]
        used_total = sum((h.value_inr for h in usable), ZERO)
        weights = {h.security_id: h.value_inr / used_total for h in usable} if used_total else {}
        dates = _common(*(closes[i] for i in ids)) if ids else []
        if not held:
            nothing = "no holdings with a value"
        elif not ids:
            nothing = "no holding has usable bars"
        else:
            nothing = ""
        vol = na(nothing) if nothing else _volatility(ids, weights, closes, dates, cfg)
        drawdowns = {
            f"{y}y": na(nothing) if nothing else _drawdown(y, ids, weights, closes, dates)
            for y in cfg.drawdown_years
        }
        cand_closes = _closes(bars.get(candidate.security_id, ()), as_of) if candidate else None
        benches = {m: _closes(b, as_of) for m, b in benchmarks.items()}
        lines: list[HoldingRisk] = []
        betas: list[tuple[RiskHolding, Metric]] = []
        for h in held:
            beta = _beta(h, closes[h.security_id], benches.get(h.market), cfg)
            betas.append((h, beta))
            lines.append(
                HoldingRisk(
                    h.security_id,
                    h.name,
                    _pct(h.value_inr, total),
                    beta,
                    _correlation(closes[h.security_id], cand_closes, cfg),
                    _days_to_trade(
                        bars.get(h.security_id, ()), h.value_inr, h.inr_per_unit, as_of, cfg
                    ),
                )
            )
        have = [(h, m) for h, m in betas if m.available and m.value is not None]
        if have:
            base = sum((h.value_inr for h, _ in have), ZERO)
            mixed = sum(((m.value or ZERO) * h.value_inr for h, m in have), ZERO) / base
            portfolio_beta = ok(_q(mixed), coverage_pct=_pct(base, total))
        else:
            portfolio_beta = na(betas[0][1].reason or "no beta" if betas else nothing or "no beta")
        cand_days = None
        pro_forma = None
        if candidate is not None:
            size = candidate.proposed_weight_pct / HUNDRED * total
            cand_days = _days_to_trade(
                bars.get(candidate.security_id, ()), size, candidate.inr_per_unit, as_of, cfg
            )
            if total:
                pro_forma = _pro_forma(held, total, candidate, max_position_pct, max_sector_pct)
        last = max((max(c) for c in closes.values() if c), default=None)
        return RiskResult(
            as_of or last,
            total,
            _pct(used_total, total) if total else None,
            excluded,
            vol,
            drawdowns,
            portfolio_beta,
            tuple(lines),
            cand_days,
            pro_forma,
        )
