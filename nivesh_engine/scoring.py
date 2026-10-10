"""Composite factor scoring (ST-6.9): quality, value, growth and revisions, momentum, risk.

Each factor is the mean of the percentile scores (0 to 100) of its available sub-inputs. A
percentile is the mid-rank of the security within its cohort: the same market and sector, or the
whole market when the sector holds fewer than `min_peers_for_sector` names. A cohort of one scores
50, never 100. Long-term and positional horizons weight the five factors differently; the weights
carry a version label and a digest that are emitted with every score.

The BR-12 rule (a recommendation never rests on recent price alone) holds by construction:
  * `support` is the mean of the quality, value and growth factors, which read no price history
    except the price paid in the valuation multiples;
  * the momentum credit is `min(momentum, support)`, less a penalty when the valuation percentile
    is over the configured limit, and zero when there is no support at all;
  * the price-derived risk sub-inputs are held to the support in the same way;
  * the top band needs a composite at `top_band_min` and a support at `support_floor`.
A hard red flag sets `cap = "HOLD"` and clamps the band; a name with fewer than `min_input_pct`
of its inputs is `insufficient_data` and has no composite. Pure and Decimal-only; the sub-inputs
come from the shared metric registry, so a screen rule and a score read the same figures.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

from nivesh_core.analysis_config import FACTORS, AnalysisSettings
from nivesh_engine import dmath
from nivesh_engine.metrics import Bundles, Cell, SecurityInputs, evaluate
from nivesh_engine.redflags import FlagResult

Horizon = Literal["long_term", "positional"]
HORIZONS = ("long_term", "positional")
QUANTUM = Decimal("0.0001")
HUNDRED = dmath.HUNDRED
TOP, UPPER, HOLD, WEAK, INSUFFICIENT = "top", "upper", "hold", "weak", "insufficient_data"
CAP_HOLD = "HOLD"
FLAG_HEALTH = "flag_health"


@dataclass(frozen=True)
class SubInput:
    """One figure a factor reads. `absolute` inputs are already 0 to 100 scores and are not ranked
    against a cohort; `price_derived` inputs are built from price history."""

    name: str
    factor: str
    higher_is_better: bool
    price_derived: bool
    absolute: bool = False


def _sub(
    names: str, factor: str, higher: bool, *, price: bool = False, absolute: bool = False
) -> list[SubInput]:
    return [SubInput(n, factor, higher, price, absolute) for n in names.split()]


SUB_INPUTS: tuple[SubInput, ...] = (
    *_sub("return_on_capital cfo_to_pat net_margin_pct", "quality", True),
    *_sub("debt_equity revenue_growth_stdev_pp", "quality", False),
    *_sub("fcf_yield_pct", "value", True),
    *_sub("valuation_percentile pe_vs_peer_median_pct ev_ebitda", "value", False),
    *_sub("revenue_cagr eps_cagr net_income_cagr", "growth", True),
    *_sub("revisions_direction", "growth", True, absolute=True),
    *_sub("price_vs_sma200_pct rs_benchmark_change_6m_pct", "momentum", True, price=True),
    *_sub("setup_quality", "momentum", True, price=True, absolute=True),
    *_sub("vol_60d max_drawdown_pct atr_pct", "risk", False, price=True),
    *_sub(FLAG_HEALTH, "risk", True, absolute=True),
)
PRICE_DERIVED = frozenset(s.name for s in SUB_INPUTS if s.price_derived)
SUPPORT_FACTORS = ("quality", "value", "growth")
BAND_RANK = {WEAK: 0, HOLD: 1, UPPER: 2, TOP: 3}


@dataclass(frozen=True)
class SecurityMeta:
    market: str
    sector: str | None = None


@dataclass(frozen=True)
class ScoreCard:
    security_id: int
    horizon: str
    factors: Mapping[str, Decimal | None] = field(default_factory=dict)
    composite: Decimal | None = None
    band: str = INSUFFICIENT
    cap: str | None = None
    weights_version: str = ""
    weights_digest: str = ""
    inputs_available: int = 0
    inputs_total: int = 0
    support: Decimal | None = None
    momentum_credit: Decimal | None = None
    reasons: tuple[str, ...] = ()


def _clamp(v: Decimal) -> Decimal:
    return min(HUNDRED, max(Decimal(0), v))


def _cohort_scores(
    raw: Mapping[int, Mapping[str, Decimal | None]],
    meta: Mapping[int, SecurityMeta],
    cfg: AnalysisSettings,
) -> tuple[dict[int, dict[str, Decimal]], dict[int, list[str]]]:
    """Percentile score per security and ranked sub-input, and the cohort fallbacks by security."""
    scores: dict[int, dict[str, Decimal]] = {sid: {} for sid in raw}
    fallback: dict[int, dict[str, list[str]]] = {sid: {} for sid in raw}
    need = cfg.scoring.min_peers_for_sector
    for sub in SUB_INPUTS:
        if sub.absolute:
            continue
        by_market: dict[str, list[Decimal]] = {}
        by_sector: dict[tuple[str, str], list[Decimal]] = {}
        for sid in sorted(raw):
            v = raw[sid].get(sub.name)
            if v is None:
                continue
            m = meta[sid]
            by_market.setdefault(m.market, []).append(v)
            if m.sector:
                by_sector.setdefault((m.market, m.sector), []).append(v)
        for sid in sorted(raw):
            v = raw[sid].get(sub.name)
            if v is None:
                continue
            m = meta[sid]
            cohort = by_market[m.market]
            sector = by_sector.get((m.market, m.sector or ""), [])
            if m.sector and len(sector) >= need:
                cohort = sector
            elif m.sector:
                why = f"sector {m.sector!r} has fewer than {need} peers"
                fallback[sid].setdefault(why, []).append(sub.name)
            else:
                fallback[sid].setdefault("no sector recorded", []).append(sub.name)
            rank = dmath.percentile_rank(v, cohort)
            if rank is None:  # the cohort always holds this security
                continue
            scores[sid][sub.name] = rank if sub.higher_is_better else HUNDRED - rank
    notes = {
        sid: [
            f"market cohort used ({why}; min_peers_for_sector={need}) for {', '.join(names)}"
            for why, names in sorted(found.items())
        ]
        for sid, found in fallback.items()
    }
    return scores, notes


def _flag_health(flags: Sequence[FlagResult] | None, cfg: AnalysisSettings) -> Decimal | None:
    """100 less a penalty per fired flag; None unless at least one flag could be evaluated (a
    flag that cannot be tested never reassures)."""
    if not flags or all(f.status == "not_evaluable" for f in flags):
        return None
    hard = sum(1 for f in flags if f.status == "fired" and f.severity == "hard")
    soft = sum(1 for f in flags if f.status == "fired" and f.severity != "hard")
    s = cfg.scoring
    return _clamp(HUNDRED - hard * s.flag_penalty_hard - soft * s.flag_penalty_soft)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return dmath.mean(values) if values else None


def _q(v: Decimal | None) -> Decimal | None:
    return None if v is None else dmath.quantize(v, QUANTUM)


def _band(composite: Decimal, support: Decimal | None, cfg: AnalysisSettings) -> tuple[str, str]:
    s = cfg.scoring
    if composite >= s.top_band_min:
        if support is not None and support >= s.support_floor:
            return TOP, ""
        had = "no fundamental support" if support is None else f"support {_q(support)}"
        return UPPER, f"top band withheld: {had} is below support_floor {s.support_floor}"
    if composite >= s.upper_band_min:
        return UPPER, ""
    return (HOLD if composite >= s.hold_band_min else WEAK), ""


def score_universe(
    raw: Mapping[int, Mapping[str, Decimal | None]],
    meta: Mapping[int, SecurityMeta],
    flags: Mapping[int, Sequence[FlagResult] | None],
    *,
    horizon: Horizon,
    cfg: AnalysisSettings,
) -> dict[int, ScoreCard]:
    """Score every security in `raw` (sub-input name -> value, None when unavailable). The
    cohort of each percentile is drawn from the securities supplied."""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(HORIZONS)}")
    scores, notes = _cohort_scores(raw, meta, cfg)
    with localcontext(dmath.CONTEXT):
        return {
            sid: _score_one(sid, raw[sid], scores[sid], notes[sid], flags.get(sid), horizon, cfg)
            for sid in sorted(raw)
        }


def _score_one(
    sid: int,
    values: Mapping[str, Decimal | None],
    ranked: dict[str, Decimal],
    notes: list[str],
    flags: Sequence[FlagResult] | None,
    horizon: str,
    cfg: AnalysisSettings,
) -> ScoreCard:
    s = cfg.scoring
    weights = getattr(s.weights, horizon)
    total = len(SUB_INPUTS)
    sub = dict(ranked)
    for spec in SUB_INPUTS:
        given = values.get(spec.name)
        if spec.absolute and spec.name != FLAG_HEALTH and given is not None:
            sub[spec.name] = _clamp(given)
    health = _flag_health(flags, cfg)
    if health is not None:
        sub[FLAG_HEALTH] = health
    reasons = list(notes)
    available = len(sub)

    def of_factor(factor: str, *, price: bool | None = None) -> list[Decimal]:
        return [
            sub[x.name]
            for x in SUB_INPUTS
            if x.factor == factor and x.name in sub and price in (None, x.price_derived)
        ]

    plain = {f: _mean(of_factor(f)) for f in SUPPORT_FACTORS}
    support = _mean([v for v in plain.values() if v is not None])
    held = support if support is not None else Decimal(0)
    momentum = _mean(of_factor("momentum"))
    credit: Decimal | None = None
    if momentum is not None:
        credit = min(momentum, held)
        pct = values.get("valuation_percentile")
        if pct is not None and pct > s.valuation_penalty_over_pct:
            credit = max(Decimal(0), credit - s.valuation_penalty_points)
            reasons.append(
                f"valuation percentile {_q(pct)} is over {s.valuation_penalty_over_pct}: "
                f"momentum credit reduced by {s.valuation_penalty_points}"
            )
        if support is None:
            reasons.append("no fundamental factor is available: no momentum credit")
    risk_inputs = [min(v, held) for v in of_factor("risk", price=True)]
    risk_inputs += of_factor("risk", price=False)
    factors: dict[str, Decimal | None] = {
        "quality": plain["quality"], "value": plain["value"], "growth": plain["growth"],
        "momentum": credit, "risk": _mean(risk_inputs),
    }  # fmt: skip
    hard = any(f.status == "fired" and f.severity == "hard" for f in flags or ())
    cap = CAP_HOLD if hard else None
    if hard:
        reasons.append("hard red flag fired: cap HOLD")
    shown = {f: _q(factors[f]) for f in FACTORS}
    live = [(f, v) for f, v in factors.items() if v is not None]
    if not live or available * 100 < s.min_input_pct * total:
        reasons.append(
            f"insufficient data: {available} of {total} inputs available, {s.min_input_pct}% needed"
        )
        return ScoreCard(
            sid, horizon, shown, None, INSUFFICIENT, cap, s.weights_version, s.weights_digest(),
            available, total, _q(support), _q(credit), tuple(reasons),
        )  # fmt: skip
    wsum = sum((getattr(weights, f) for f, _ in live), Decimal(0))
    composite = sum((getattr(weights, f) * v for f, v in live), Decimal(0)) / wsum
    band, why = _band(composite, support, cfg)
    if why:
        reasons.append(why)
    if hard and BAND_RANK[band] > BAND_RANK[HOLD]:
        band = HOLD
    return ScoreCard(
        sid, horizon, shown, _q(composite), band, cap, s.weights_version, s.weights_digest(),
        available, total, _q(support), _q(credit), tuple(reasons),
    )  # fmt: skip


# ---- reading the sub-inputs out of the metric registry ----------------------------------------
def _value(cell: Cell) -> Decimal | None:
    return cell.value if isinstance(cell.value, Decimal) else None


def _first(b: Bundles, *names: str) -> Decimal | None:
    for name in names:
        v = _value(evaluate(name, b))
        if v is not None:
            return v
    return None


def _drawdown(inp: SecurityInputs, as_of: date, cfg: AnalysisSettings) -> Decimal | None:
    """Largest peak-to-trough fall of the close, in percent, over the last `risk.lookback_days`
    bars on or before `as_of`; None with fewer bars than the longest volatility window."""
    closes = [b.close for b in inp.bars if b.date <= as_of and b.close is not None]
    closes = closes[-cfg.risk.lookback_days :]
    if len(closes) < max(cfg.ta.vol_windows):
        return None
    peak, worst = closes[0], Decimal(0)
    with localcontext(dmath.CONTEXT):
        for c in closes:
            peak = max(peak, c)
            if peak > 0:
                worst = max(worst, (peak - c) / peak * 100)
    return dmath.quantize(worst, QUANTUM)


def _direction(cell: Cell) -> Decimal | None:
    v = _value(cell)
    if v is None:
        return None
    return Decimal(100) if v > 0 else (Decimal(50) if v == 0 else Decimal(0))


def raw_inputs(
    universe: Mapping[int, SecurityInputs], *, as_of: date, cfg: AnalysisSettings
) -> tuple[
    dict[int, dict[str, Decimal | None]],
    dict[int, SecurityMeta],
    dict[int, list[FlagResult] | None],
]:
    """The sub-inputs, the cohort keys and the red-flag results of each security, as of a date,
    read through the same bundles and registry the screener uses."""
    raw: dict[int, dict[str, Decimal | None]] = {}
    meta: dict[int, SecurityMeta] = {}
    flags: dict[int, list[FlagResult] | None] = {}
    for sid in sorted(universe):
        inp = universe[sid]
        b = Bundles(inp, as_of, cfg)
        spread = b.fa().metrics.get("revenue_growth_stdev_pp")
        setup = b.setup().setup
        raw[sid] = {
            "return_on_capital": _first(b, "roce", "roic", "roe"),
            "cfo_to_pat": _first(b, "cfo_to_pat"),
            "net_margin_pct": _first(b, "net_margin_pct"),
            "debt_equity": _first(b, "debt_equity"),
            "revenue_growth_stdev_pp": spread.value if spread and spread.available else None,
            "fcf_yield_pct": _first(b, "fcf_yield_pct"),
            "valuation_percentile": _first(b, "valuation_percentile"),
            "pe_vs_peer_median_pct": _first(b, "pe_vs_peer_median_pct"),
            "ev_ebitda": _first(b, "ev_ebitda"),
            "revenue_cagr": _first(b, "revenue_cagr"),
            "eps_cagr": _first(b, "eps_cagr"),
            "net_income_cagr": _first(b, "net_income_cagr"),
            "revisions_direction": _direction(evaluate("revisions", b)),
            "price_vs_sma200_pct": _first(b, "price_vs_sma200_pct"),
            "rs_benchmark_change_6m_pct": _first(b, "rs_benchmark_change_6m_pct"),
            "setup_quality": cfg.scoring.setup_quality.get(setup) if setup else None,
            "vol_60d": _first(b, "vol_60d"),
            "max_drawdown_pct": _drawdown(inp, as_of, cfg),
            "atr_pct": _first(b, "atr_pct"),
        }
        meta[sid] = SecurityMeta(inp.market, inp.sector)
        flags[sid] = b.flags()
    return raw, meta, flags


def ranking(cards: Mapping[int, ScoreCard]) -> list[int]:
    """Security ids, best composite first; ties go to the lower id; no composite goes last."""
    return sorted(
        cards,
        key=lambda sid: (
            cards[sid].composite is None,
            -(cards[sid].composite or Decimal(0)),
            sid,
        ),
    )
