"""The metric registry shared by the screener (ST-6.8) and the scoring (ST-6.9).

Every metric a rule or a factor can read is named here once: which bundle computes it (technical
indicators, fundamentals, valuation, estimates, red flags, setups or the supplied universe), which
stored inputs it needs, and how its value is read out of the bundle. A bundle is computed lazily and
at most once per security, so a rule set that reads only fundamentals never touches the bars.
A metric that cannot be read is a `Cell` with no value and a reason, never zero. The registry holds
the ST-6.8 metrics and the ST-9.2 preset metrics, so a preset is a rule file and nothing more.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, localcontext
from functools import cache

from nivesh_core.analysis_config import AnalysisSettings, TaSettings
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.market_store import EstimateRow
from nivesh_engine import dmath
from nivesh_engine.bars import Bar
from nivesh_engine.fa import FaResult, fa_compute
from nivesh_engine.metric import Metric
from nivesh_engine.redflags import FlagInputs, FlagResult, detect_flags
from nivesh_engine.setups import Setup, classify_setup
from nivesh_engine.statements import StatementRow
from nivesh_engine.ta import TaResult, rs_percentiles, ta_compute
from nivesh_engine.valuation import (
    PeerMultiples,
    ValuationInputs,
    ValuationResult,
    valuation_multiples,
)

QUANTUM = Decimal("0.00000001")
REVISION_METRIC = "eps"
RS_PREFIX = "rs_benchmark_change_"
# cheap bundles first: a security that fails an inexpensive rule never reaches the expensive ones
BUNDLE_COST = {
    "fa": 1,
    "estimates": 1,
    "flags": 2,
    "ta": 3,
    "valuation": 4,
    "setup": 5,
    "universe": 6,
}


@dataclass(frozen=True)
class SecurityInputs:
    """Everything the bundles read for one security. A missing input makes the metrics that need it
    unavailable with a reason; it never raises."""

    security_id: int
    symbol: str
    market: str = "US"
    sector: str | None = None
    sector_kind: str = "general"
    bars: Sequence[Bar] = ()
    benchmark: Sequence[Bar] | None = None
    sector_bars: Sequence[Bar] | None = None
    rows: Sequence[StatementRow] = ()
    shareholding: Sequence[ShareholdingRow] = ()
    valuation: ValuationInputs | None = None
    peers: PeerMultiples | None = None
    estimates: Sequence[EstimateRow] = ()  # snapshots of the analyst estimates, any dates
    flags: FlagInputs | None = None


@dataclass(frozen=True)
class Cell:
    """A metric read out: a number, a text, or no value and the reason."""

    value: Decimal | str | None
    reason: str | None = None
    inputs: Mapping[str, object] = field(default_factory=dict)


def cell_of(m: Metric) -> Cell:
    if m.available and m.value is not None:
        return Cell(m.value, None, m.inputs)
    return Cell(None, m.reason or "unavailable", m.inputs)


def _gone(reason: str) -> Cell:
    return Cell(None, reason)


_KNOWN_TA: dict[str, frozenset[str]] = {}


def _known_ta(cfg: TaSettings) -> frozenset[str]:
    """The indicator names `ta_compute` produces under this configuration."""
    key = cfg.model_dump_json()
    if key not in _KNOWN_TA:
        _KNOWN_TA[key] = frozenset(ta_compute((), as_of=date.min, cfg=cfg).values)
    return _KNOWN_TA[key]


def _rs_name(cfg: TaSettings) -> str:
    return next(n for n in sorted(_known_ta(cfg)) if n.startswith(RS_PREFIX) and "sector" not in n)


class UniverseContext:
    """Facts that depend on the whole supplied universe: the relative-strength percentile ranks."""

    def __init__(
        self, universe: Mapping[int, SecurityInputs], as_of: date, cfg: AnalysisSettings
    ) -> None:
        self.universe, self.as_of, self.cfg = universe, as_of, cfg
        self._changes: dict[int, Metric] | None = None
        self._ranks: dict[int, Decimal | None] = {}

    def rs_percentile(self, security_id: int) -> Cell:
        if self._changes is None:
            name = _rs_name(self.cfg.ta)
            self._changes = {
                sid: ta_compute(
                    inp.bars,
                    as_of=self.as_of,
                    benchmark=inp.benchmark,
                    cfg=self.cfg.ta,
                    only={name},
                ).values[name]
                for sid, inp in sorted(self.universe.items())
            }
            self._ranks = rs_percentiles(
                {sid: m.value if m.available else None for sid, m in self._changes.items()}
            )
        rank = self._ranks.get(security_id)
        if rank is None:
            m = self._changes.get(security_id)
            return _gone(m.reason or "unavailable" if m else "not in the universe")
        return Cell(
            dmath.quantize(rank, QUANTUM),
            None,
            {
                "universe": len(self.universe),
                "ranked": sum(1 for r in self._ranks.values() if r is not None),
            },
        )


class Bundles:
    """The lazily computed bundles of one security. `plan` (bundle -> metric names the caller will
    read) lets the technical bundle compute only the indicators that are asked for."""

    def __init__(
        self,
        inp: SecurityInputs,
        as_of: date,
        cfg: AnalysisSettings,
        universe: UniverseContext | None = None,
        plan: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        self.inp, self.as_of, self.cfg = inp, as_of, cfg
        self._universe = universe
        self._plan = plan or {}
        self._memo: dict[str, object] = {}

    def universe(self) -> UniverseContext:
        if self._universe is None:
            self._universe = UniverseContext({self.inp.security_id: self.inp}, self.as_of, self.cfg)
        return self._universe

    def ta(self) -> TaResult:
        if "ta" not in self._memo:
            wanted = self._plan.get("ta")
            only = None if wanted is None else set(wanted) & _known_ta(self.cfg.ta)
            self._memo["ta"] = ta_compute(
                self.inp.bars, as_of=self.as_of, benchmark=self.inp.benchmark,
                sector=self.inp.sector_bars, cfg=self.cfg.ta, only=only,
            )  # fmt: skip
        return self._memo["ta"]  # type: ignore[return-value]

    def fa(self) -> FaResult:
        if "fa" not in self._memo:
            self._memo["fa"] = fa_compute(
                self.inp.rows, self.inp.shareholding, as_of=self.as_of,
                sector_kind=self.inp.sector_kind, cfg=self.cfg, market=self.inp.market,
            )  # fmt: skip
        return self._memo["fa"]  # type: ignore[return-value]

    def valuation(self) -> ValuationResult | None:
        if "valuation" not in self._memo:
            v = self.inp.valuation
            self._memo["valuation"] = (
                None if v is None else valuation_multiples(v, self.inp.peers, cfg=self.cfg)
            )
        return self._memo["valuation"]  # type: ignore[return-value]

    def flags(self) -> list[FlagResult] | None:
        if "flags" not in self._memo:
            f = self.inp.flags
            self._memo["flags"] = (
                None if f is None else detect_flags(f, as_of=self.as_of, cfg=self.cfg)
            )
        return self._memo["flags"]  # type: ignore[return-value]

    def setup(self) -> Setup:
        if "setup" not in self._memo:
            self._memo["setup"] = classify_setup(
                self.inp.bars, as_of=self.as_of, cfg=self.cfg.setups,
                levels_cfg=self.cfg.levels, ta_cfg=self.cfg.ta,
            )  # fmt: skip
        return self._memo["setup"]  # type: ignore[return-value]


@dataclass(frozen=True)
class MetricDef:
    name: str
    bundle: str
    kind: str  # "numeric" or "string"
    needs: frozenset[str]  # the stored inputs the bundle reads
    extract: Callable[[Bundles], Cell]


def _ta_metric(name: str, needs: Iterable[str] = ("bars",)) -> MetricDef:
    def extract(b: Bundles) -> Cell:
        m = b.ta().values.get(name)
        return (
            _gone(f"{name} is not computed with the configured windows")
            if m is None
            else cell_of(m)
        )

    return MetricDef(name, "ta", "numeric", frozenset(needs), extract)


def _fa_metric(
    name: str,
    fa_name: str | Callable[[AnalysisSettings], str],
    needs: Iterable[str] = ("statements",),
) -> MetricDef:
    def extract(b: Bundles) -> Cell:
        target = fa_name(b.cfg) if callable(fa_name) else fa_name
        m = b.fa().metrics.get(target)
        if m is None:
            return _gone(f"{name} is not reported for {b.inp.sector_kind} companies")
        return cell_of(m)

    return MetricDef(name, "fa", "numeric", frozenset(needs), extract)


def _cagr(item: str) -> Callable[[AnalysisSettings], str]:
    return lambda cfg: f"{item}_cagr_{cfg.fa.growth_years}y_pct"


VALUATION_NEEDS = ("statements", "last_close")


def _valuation_metric(name: str, multiple: str) -> MetricDef:
    def extract(b: Bundles) -> Cell:
        result = b.valuation()
        return (
            _gone("valuation inputs not loaded")
            if result is None
            else cell_of(result.multiples[multiple].current)
        )

    return MetricDef(name, "valuation", "numeric", frozenset(VALUATION_NEEDS), extract)


def _valuation_percentile(b: Bundles) -> Cell:
    result = b.valuation()
    if result is None:
        return _gone("valuation inputs not loaded")
    window = f"{b.cfg.valuation.history_years[0]}y"
    why: list[str] = []
    for multiple in ("pe", "pb"):
        m = result.multiples[multiple].percentiles[window]
        if m.available and m.value is not None:
            return Cell(m.value, None, {**m.inputs, "multiple": multiple, "window": window})
        why.append(f"{multiple}: {m.reason}")
    return _gone("; ".join(why))


def _peer_discount(b: Bundles) -> Cell:
    result = b.valuation()
    if result is None:
        return _gone("valuation inputs not loaded")
    pe = result.multiples["pe"]
    if not (pe.current.available and pe.current.value is not None):
        return _gone(pe.current.reason or "no P/E")
    med = pe.peer_median
    if not (med.available and med.value):
        return _gone(med.reason or "peer median P/E is zero")
    with localcontext(dmath.CONTEXT):
        return Cell(
            dmath.quantize((pe.current.value / med.value - 1) * 100, QUANTUM), None, med.inputs
        )


def _revisions(b: Bundles) -> Cell:
    inp = b.inp
    rows = [r for r in inp.estimates if r.metric == REVISION_METRIC and r.as_of <= b.as_of]
    if not rows:
        if inp.market == "IN":
            return _gone("no estimates source for India (revisions unavailable)")
        return _gone("no stored estimates")
    newest = max(rows, key=lambda r: (r.as_of, r.period))
    series = sorted((r for r in rows if r.period == newest.period), key=lambda r: r.as_of)
    since = b.as_of - timedelta(days=b.cfg.screen.revision_lookback_days)
    base = next((r for r in series if r.as_of >= since), None)
    if base is None or base.as_of == series[-1].as_of:
        return _gone(
            f"only one estimate snapshot in the last {b.cfg.screen.revision_lookback_days} days"
        )
    if base.value == 0:
        return _gone("the earlier EPS estimate is zero")
    with localcontext(dmath.CONTEXT):
        change = dmath.quantize((series[-1].value / base.value - 1) * 100, QUANTUM)
    return Cell(change, None, {"period": newest.period, "from": base.as_of.isoformat()})


def _flag_count(severity: str) -> Callable[[Bundles], Cell]:
    def extract(b: Bundles) -> Cell:
        results = b.flags()
        if results is None:
            return _gone("flag inputs not loaded")
        evaluable = [r for r in results if r.status != "not_evaluable"]
        if not evaluable:
            return _gone("no flag could be evaluated")
        hits = [r.flag for r in evaluable if r.status == "fired" and r.severity == severity]
        return Cell(
            Decimal(len(hits)), None, {"flags": ", ".join(hits), "evaluable": len(evaluable)}
        )

    return extract


def _setup_type(b: Bundles) -> Cell:
    s = b.setup()
    return (
        _gone(s.reason or "setup unavailable")
        if s.setup is None
        else Cell(s.setup, None, {"as_of": str(s.as_of)})
    )


def _setup_flag(kind: str) -> Callable[[Bundles], Cell]:
    def extract(b: Bundles) -> Cell:
        s = b.setup()
        if s.setup is None:
            return _gone(s.reason or "setup unavailable")
        return Cell(Decimal(1 if kind in s.matched else 0), None, {"matched": ", ".join(s.matched)})

    return extract


TA_METRICS = (
    "sma_20", "sma_50", "sma_200", "ema_21", "slope_50dma_pct", "price_vs_sma200_pct", "rsi_14",
    "macd_hist", "roc_3m", "roc_6m", "roc_12m", "dist_52w_high_pct", "dist_52w_low_pct", "atr_pct",
    "vol_20d", "vol_60d", "avg_volume_20", "updown_volume_ratio_20",
)  # fmt: skip
FA_PASS_THROUGH = (
    "gross_margin_pct", "operating_margin_pct", "ebitda_margin_pct", "net_margin_pct",
    "debt_equity", "net_debt_ebitda", "interest_cover", "current_ratio", "cfo_to_pat",
    "fcf_margin_pct", "gnpa_pct", "nnpa_pct", "nim_pct", "casa_pct", "car_pct",
)  # fmt: skip


def _build() -> dict[str, MetricDef]:
    defs: list[MetricDef] = [_ta_metric(n) for n in TA_METRICS]
    defs.append(_ta_metric("rs_benchmark_change_6m_pct", ("bars", "benchmark")))
    defs.append(_ta_metric("rs_sector_change_6m_pct", ("bars", "sector_bars")))
    defs += [_fa_metric(n, n) for n in FA_PASS_THROUGH]
    defs += [
        _fa_metric("roce", "roce_pct"),
        _fa_metric("roe", "roe_pct"),
        _fa_metric("roic", "roic_pct"),
        _fa_metric("revenue_cagr", _cagr("revenue")),
        _fa_metric("net_income_cagr", _cagr("net_income")),
        _fa_metric("eps_cagr", _cagr("eps")),
        _fa_metric("promoter_pledged_pct", "promoter_pledged_pct", ("statements", "shareholding")),
        _fa_metric(
            "promoter_change_4q_pp", "promoter_change_4q_pp", ("statements", "shareholding")
        ),
    ]
    defs += [
        _valuation_metric(n, n)
        for n in ("pe", "pb", "ev_ebitda", "fcf_yield_pct", "dividend_yield_pct")
    ]
    defs.append(
        MetricDef(
            "valuation_percentile",
            "valuation",
            "numeric",
            frozenset(("statements", "closes")),
            _valuation_percentile,
        )
    )
    defs.append(
        MetricDef(
            "pe_vs_peer_median_pct",
            "valuation",
            "numeric",
            frozenset((*VALUATION_NEEDS, "peers")),
            _peer_discount,
        )
    )
    defs.append(
        MetricDef("revisions", "estimates", "numeric", frozenset(("estimates",)), _revisions)
    )
    flag_needs = ("statements", "shareholding", "filings")
    defs.append(
        MetricDef("hard_flag_count", "flags", "numeric", frozenset(flag_needs), _flag_count("hard"))
    )
    defs.append(
        MetricDef("soft_flag_count", "flags", "numeric", frozenset(flag_needs), _flag_count("soft"))
    )
    defs.append(MetricDef("setup_type", "setup", "string", frozenset(("bars",)), _setup_type))
    defs.append(
        MetricDef(
            "base_breakout", "setup", "numeric", frozenset(("bars",)), _setup_flag("breakout")
        )
    )
    defs.append(
        MetricDef(
            "pullback_to_50dma", "setup", "numeric", frozenset(("bars",)), _setup_flag("pullback")
        )
    )
    defs.append(
        MetricDef(
            "rs_percentile",
            "universe",
            "numeric",
            frozenset(("bars", "benchmark")),
            lambda b: b.universe().rs_percentile(b.inp.security_id),
        )
    )
    return {d.name: d for d in defs}


REGISTRY: dict[str, MetricDef] = _build()


def inputs_needed(names: Iterable[str]) -> frozenset[str]:
    """The stored inputs the named metrics read (the loader loads only these)."""
    out: set[str] = set()
    for name in names:
        out |= REGISTRY[name].needs
    return frozenset(out)


def evaluate(name: str, bundles: Bundles) -> Cell:
    return REGISTRY[name].extract(bundles)


def value_of(m: Metric | Cell) -> Decimal | None:
    """The Decimal value of an available metric or cell, else None."""
    v = m.value
    ok = m.available if isinstance(m, Metric) else True
    return v if ok and isinstance(v, Decimal) else None


def metric_values(b: Bundles) -> dict[str, Decimal | None]:
    """Every numeric engine figure for one security by name (FA, TA, valuation multiples and
    their own-history percentiles, and the screener metrics); None when unavailable."""
    out: dict[str, Decimal | None] = {}
    for name, m in (*b.fa().metrics.items(), *b.ta().values.items()):
        out[name] = value_of(m)
    v = b.valuation()
    for name, mr in v.multiples.items() if v is not None else ():
        out[name] = value_of(mr.current)
        for window, m in mr.percentiles.items():
            out[f"{name}_percentile_{window}"] = value_of(m)
    for name, d in REGISTRY.items():
        if d.kind == "numeric" and d.bundle != "universe":
            out[name] = value_of(evaluate(name, b))
    return dict(sorted(out.items()))


@cache
def known_metrics() -> frozenset[str]:
    """The names `metric_values` produces under the default analysis settings: the only valid
    `metric` of a kill criterion. Computed once from empty inputs, so it cannot drift."""
    day = date(2000, 1, 1)
    inp = SecurityInputs(0, "", valuation=ValuationInputs("IN", "general", day, (), (), None))
    return frozenset(metric_values(Bundles(inp, day, AnalysisSettings())))
