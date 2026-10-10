"""Fund doctor (ST-5.6): one of KEEP / SWITCH_TO_DIRECT / REPLACE / CONSOLIDATE / REVIEW per fund,
with reason codes that cite the metric, its value and the threshold.

Pure and deterministic: no I/O, no clock, no model. Thresholds come from `MfSettings` (owner-set).
Rules run in a fixed sequence and the first one that fires decides the action; every reason of every
firing rule is reported, sorted by (rule, reason code, metric).

  1. regular plan with a found direct twin and a TER gap above the threshold -> SWITCH_TO_DIRECT
  2. overlap with an owned same-category fund above the threshold             -> CONSOLIDATE
  3. consistency below the minimum, or downside capture / drawdown beyond the
     maximum                                                                  -> REPLACE when a
     screened candidate exists, else REVIEW
  4. a short manager tenure, a stretched valuation, an ambiguous direct twin, a fund that is not
     comparable, or a core metric that is unavailable                        -> REVIEW
  otherwise KEEP, citing the metrics that passed.

BR-12: a trailing one-year return is accepted as display-only input and no rule reads it, so it can
never be the reason for an action. An unavailable metric is listed as such, never treated as a
pass. Exit-load and tax-impact blocks are computed (from the lots) only for SWITCH_TO_DIRECT and
REPLACE, before the action is shown. Scheme names are untrusted external text: they are carried as
data and never interpolated into an instruction.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from nivesh_core.config import MfSettings
from nivesh_engine.mf_cost import TwinResult
from nivesh_engine.mf_lots import (
    ExitLoadResult,
    TaxImpact,
    ValuedLots,
    exit_load_estimate,
    tax_impact,
)

DEFERRED = ["agent reasoning and /review-funds command (ST-7.5, not built)"]
CORE_METRICS = ("beat_pct", "median_excess_pct", "downside_capture_pct", "max_drawdown_pct")


class Action(StrEnum):
    KEEP = "KEEP"
    SWITCH_TO_DIRECT = "SWITCH_TO_DIRECT"
    REPLACE = "REPLACE"
    CONSOLIDATE = "CONSOLIDATE"
    REVIEW = "REVIEW"


class ReasonCode(StrEnum):
    BEAT_PCT_BELOW_MIN = "BEAT_PCT_BELOW_MIN"
    DIRECT_TWIN_AMBIGUOUS = "DIRECT_TWIN_AMBIGUOUS"
    DOWNSIDE_CAPTURE_ABOVE_MAX = "DOWNSIDE_CAPTURE_ABOVE_MAX"
    DRAWDOWN_ABOVE_MAX = "DRAWDOWN_ABOVE_MAX"
    MANAGER_TENURE_SHORT = "MANAGER_TENURE_SHORT"
    MEDIAN_EXCESS_BELOW_MIN = "MEDIAN_EXCESS_BELOW_MIN"
    METRIC_UNAVAILABLE = "METRIC_UNAVAILABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    OVERLAP_ABOVE_LIMIT = "OVERLAP_ABOVE_LIMIT"
    TER_GAP_WITH_DIRECT_TWIN = "TER_GAP_WITH_DIRECT_TWIN"
    VALUATION_STRETCH = "VALUATION_STRETCH"
    WITHIN_LIMITS = "WITHIN_LIMITS"


@dataclass(frozen=True)
class Reason:
    code: ReasonCode
    metric: str
    value: Decimal | str | None
    threshold: Decimal | None
    note: str | None = None


@dataclass(frozen=True)
class FundFacts:
    """Metrics for one fund. `trailing_1y_pct` is display-only: no rule reads it (BR-12)."""

    amfi_code: str
    name: str
    plan: str | None = None
    twin: TwinResult | None = None
    ter_gap_pct: Decimal | None = None
    overlap_pct: Decimal | None = None
    overlap_with: str | None = None
    comparable: bool = True
    beat_pct: Decimal | None = None
    median_excess_pct: Decimal | None = None
    downside_capture_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    sortino: Decimal | None = None
    tenure_years: Decimal | None = None
    valuation_ratio: Decimal | None = None
    trailing_1y_pct: Decimal | None = None
    unavailable: Mapping[str, str] = field(default_factory=dict)  # metric -> why it is None


@dataclass(frozen=True)
class Position:
    """The held lots and category, for the exit-load and tax-impact blocks."""

    lots: ValuedLots
    category: str | None


@dataclass(frozen=True)
class Verdict:
    amfi_code: str
    name: str
    exit_load: ExitLoadResult | None
    tax_impact: TaxImpact | None
    action: Action
    reasons: list[Reason]
    deferred: list[str]


Fired = tuple[list[Reason], bool]  # reasons, whether the rule fired


def _rule_switch(f: FundFacts, cfg: MfSettings) -> Fired:
    if f.plan != "regular" or f.twin is None:
        return [], False
    limit = cfg.thresholds.ter_excess_pct
    if f.twin.status == "found" and f.ter_gap_pct is not None and f.ter_gap_pct > limit:
        r = Reason(ReasonCode.TER_GAP_WITH_DIRECT_TWIN, "ter_gap_pct", f.ter_gap_pct, limit)
        return [r], True
    return [], False


def _rule_consolidate(f: FundFacts, cfg: MfSettings) -> Fired:
    limit = cfg.thresholds.overlap_pct
    if f.overlap_pct is not None and f.overlap_pct > limit:
        r = Reason(
            ReasonCode.OVERLAP_ABOVE_LIMIT, "overlap_pct", f.overlap_pct, limit, f.overlap_with
        )
        return [r], True
    return [], False


def _rule_consistency(f: FundFacts, cfg: MfSettings) -> Fired:
    th, cons = cfg.thresholds, cfg.consistency
    out: list[Reason] = []
    if f.beat_pct is not None and f.beat_pct < cons.min_beat_pct:
        out.append(Reason(ReasonCode.BEAT_PCT_BELOW_MIN, "beat_pct", f.beat_pct, cons.min_beat_pct))
    if f.median_excess_pct is not None and f.median_excess_pct < cons.min_median_excess_pct:
        out.append(
            Reason(
                ReasonCode.MEDIAN_EXCESS_BELOW_MIN,
                "median_excess_pct",
                f.median_excess_pct,
                cons.min_median_excess_pct,
            )  # fmt: skip
        )
    if f.downside_capture_pct is not None and f.downside_capture_pct > th.downside_capture_max:
        out.append(
            Reason(
                ReasonCode.DOWNSIDE_CAPTURE_ABOVE_MAX,
                "downside_capture_pct",
                f.downside_capture_pct,
                th.downside_capture_max,
            )  # fmt: skip
        )
    if f.max_drawdown_pct is not None and f.max_drawdown_pct > th.max_drawdown_pct:
        out.append(
            Reason(
                ReasonCode.DRAWDOWN_ABOVE_MAX,
                "max_drawdown_pct",
                f.max_drawdown_pct,
                th.max_drawdown_pct,
            )  # fmt: skip
        )
    return out, bool(out)


def _unavailable(f: FundFacts, metric: str) -> Reason:
    return Reason(ReasonCode.METRIC_UNAVAILABLE, metric, None, None, f.unavailable.get(metric))


def _rule_review(f: FundFacts, cfg: MfSettings) -> Fired:
    """Reasons that force REVIEW; non-core unavailable metrics are listed but do not force it."""
    th = cfg.thresholds
    out: list[Reason] = []
    forced = False
    if f.twin is not None and f.twin.status == "ambiguous" and f.plan == "regular":
        out.append(
            Reason(
                ReasonCode.DIRECT_TWIN_AMBIGUOUS,
                "direct_twin",
                ",".join(f.twin.candidates),
                None,
                f.twin.reason,
            )  # fmt: skip
        )
        forced = True
    if not f.comparable:
        out.append(
            Reason(
                ReasonCode.NOT_COMPARABLE, "comparable", "no", None, f.unavailable.get("comparable")
            )
        )
        forced = True
    else:
        for metric in CORE_METRICS:
            if getattr(f, metric) is None:
                out.append(_unavailable(f, metric))
                forced = True
    if f.plan == "regular" and f.twin is not None and f.twin.status == "found":
        if f.ter_gap_pct is None:  # rule 1 needs the gap to decide
            out.append(_unavailable(f, "ter_gap_pct"))
            forced = True
    if f.tenure_years is None:
        out.append(_unavailable(f, "tenure_years"))
    elif f.tenure_years < th.min_tenure_years:
        out.append(
            Reason(
                ReasonCode.MANAGER_TENURE_SHORT, "tenure_years", f.tenure_years, th.min_tenure_years
            )
        )
        forced = True
    if f.valuation_ratio is None:
        out.append(_unavailable(f, "valuation_ratio"))
    elif f.valuation_ratio > th.valuation_stretch_ratio:
        out.append(
            Reason(
                ReasonCode.VALUATION_STRETCH,
                "valuation_ratio",
                f.valuation_ratio,
                th.valuation_stretch_ratio,
            )  # fmt: skip
        )
        forced = True
    return out, forced


def _within_limits(f: FundFacts, cfg: MfSettings) -> list[Reason]:
    th, cons = cfg.thresholds, cfg.consistency
    pairs = [
        ("beat_pct", f.beat_pct, cons.min_beat_pct),
        ("median_excess_pct", f.median_excess_pct, cons.min_median_excess_pct),
        ("downside_capture_pct", f.downside_capture_pct, th.downside_capture_max),
        ("max_drawdown_pct", f.max_drawdown_pct, th.max_drawdown_pct),
        ("overlap_pct", f.overlap_pct, th.overlap_pct),
        ("tenure_years", f.tenure_years, th.min_tenure_years),
        ("valuation_ratio", f.valuation_ratio, th.valuation_stretch_ratio),
    ]
    return [Reason(ReasonCode.WITHIN_LIMITS, m, v, t) for m, v, t in pairs if v is not None]


def _sorted_reasons(groups: list[list[Reason]]) -> list[Reason]:
    out: list[Reason] = []
    for group in groups:  # rule order is preserved; inside a rule: reason code, then metric
        out += sorted(group, key=lambda r: (r.code.value, r.metric))
    return out


def tenure_years(since: date | None, as_of: date) -> Decimal | None:
    """Years the current manager has run the fund (actual days / 365), None when unknown."""
    if since is None or since > as_of:
        return None
    return ((as_of - since).days / Decimal(365)).quantize(Decimal("0.01"))


def diagnose(
    facts: FundFacts,
    cfg: MfSettings,
    candidates_available: bool,
    position: Position | None = None,
) -> Verdict:
    """Action and reasons for one fund; same input gives the same verdict."""
    r1, hit1 = _rule_switch(facts, cfg)
    r2, hit2 = _rule_consolidate(facts, cfg)
    r3, hit3 = _rule_consistency(facts, cfg)
    r4, hit4 = _rule_review(facts, cfg)
    if hit1:
        action = Action.SWITCH_TO_DIRECT
    elif hit2:
        action = Action.CONSOLIDATE
    elif hit3:
        action = Action.REPLACE if candidates_available else Action.REVIEW
    elif hit4:
        action = Action.REVIEW
    else:
        action = Action.KEEP
    groups = [r1, r2, r3, r4]
    if action is Action.KEEP:
        groups = [_within_limits(facts, cfg), r4]
    exit_load = taxes = None
    if action in (Action.SWITCH_TO_DIRECT, Action.REPLACE):
        if position is None:
            gone = "no lot data for this fund"
            exit_load = ExitLoadResult(False, None, reason=gone)
            taxes = TaxImpact([], None, None, gone)
        else:
            exit_load = exit_load_estimate(position.lots, position.category, cfg.exit_load)
            taxes = tax_impact(position.lots, cfg.tax)
    return Verdict(
        facts.amfi_code,
        facts.name,
        exit_load,
        taxes,
        action,
        _sorted_reasons(groups),
        list(DEFERRED),
    )
