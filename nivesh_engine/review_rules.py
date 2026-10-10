"""Section 15.6 discipline rules and the review action floor (ST-8.2), in code.

Pure and Decimal-only: no I/O, no clock. Each of the six PID 15.6 rules is evaluated here from
facts computed elsewhere; a missing input gives `not_evaluable` with a reason, never `clear`.
The floor can only lower a proposed action along the ladder ADD > HOLD > REVIEW > TRIM > EXIT:
a met kill criterion or a triggered rule 1 or 4 caps the action at TRIM (REVIEW when an override
reason is given), and a triggered rule 2, 3 or 5 or a passed review date caps it at REVIEW.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from nivesh_core.review_config import ReviewSettings
from nivesh_core.thesis import KillCriterion

Status = Literal["met", "not_met", "unknown"]
TriggerStatus = Literal["triggered", "clear", "not_evaluable", "not_applicable"]
Action = Literal["ADD", "HOLD", "REVIEW", "TRIM", "EXIT"]
LADDER: tuple[Action, ...] = ("ADD", "HOLD", "REVIEW", "TRIM", "EXIT")  # later = more cautious
CODES = (
    "kill_criterion", "fundamental_deterioration", "valuation_stretch", "concentration",
    "below_sma200_weak_rs", "better_use_of_capital",
)  # fmt: skip
HARD = ("kill_criterion", "concentration")
SOFT = ("fundamental_deterioration", "valuation_stretch", "below_sma200_weak_rs")
SMA_WINDOW = 200
POSITIONAL = "positional_1_6m"


@dataclass(frozen=True)
class TriggerResult:
    code: str
    status: TriggerStatus
    detail: str


@dataclass(frozen=True)
class TriggerFacts:
    """Inputs of the six rules; None means unavailable. Series are oldest first."""

    criteria: tuple[tuple[int, Status], ...] | None  # merged statuses; None: no thesis
    revenue_growth: tuple[Decimal | None, ...] | None  # quarterly, percent
    operating_margin: tuple[Decimal | None, ...] | None  # quarterly, percent
    valuation_percentile: Decimal | None  # own-history percentile of the configured multiple
    revisions: Decimal | None  # estimate revisions direction
    position_weight_pct: Decimal | None
    sector_weight_pct: Decimal | None
    max_position_pct: Decimal
    max_sector_pct: Decimal
    closes: tuple[Decimal, ...] | None
    rs_change: Decimal | None  # relative-strength change; negative means weakening


@dataclass(frozen=True)
class ReviewFacts:
    """Code-computed facts about one holding, assembled before the reviewer is asked. The trigger
    facts carry no criteria yet: the review step merges them. `trim_note` names the lowest-tax
    lots and is added to the output only when the final action is TRIM."""

    security_id: int
    metrics: Mapping[str, Decimal | None]
    triggers: TriggerFacts
    tax_note: str
    trim_note: str = ""


def criterion_status(criterion: KillCriterion, value: Decimal | None) -> Status:
    """`met` when the machine metric crosses its threshold; `unknown` without a value."""
    if value is None or criterion.threshold is None or criterion.comparator is None:
        return "unknown"
    t = criterion.threshold
    hit = {"lt": value < t, "lte": value <= t, "gt": value > t, "gte": value >= t}
    return "met" if hit[criterion.comparator] else "not_met"


def merge_criterion(
    criterion: KillCriterion, code_value: Decimal | None, agent: Status
) -> tuple[Status, str | None]:
    """Code judges machine criteria when it has the value; otherwise the cautious merge keeps an
    agent `met` and turns an agent `not_met` into `unknown`. Text criteria keep the agent's call."""
    if not criterion.machine:
        return agent, None
    code = criterion_status(criterion, code_value)
    cid = criterion.criterion_id
    if code != "unknown":
        note = None if code == agent else f"criterion {cid}: code value gives {code}, not {agent}"
        return code, note
    if agent == "not_met":
        return "unknown", f"criterion {cid}: no code value to confirm not_met; kept as unknown"
    return agent, None


def sessions_below_sma(closes: Sequence[Decimal], window: int) -> int | None:
    """Trailing run of sessions closing under their own `window`-session mean."""
    if len(closes) < window:
        return None
    total = sum(closes[-window:], Decimal(0))
    run = 0
    for i in range(len(closes) - 1, window - 2, -1):
        if closes[i] * window >= total:
            break
        run += 1
        if i - window >= 0:
            total += closes[i - window] - closes[i]
    return run


def declining_quarters(series: Sequence[Decimal | None], n: int) -> bool | None:
    """True when each of the last `n` points is below the one before it."""
    tail = list(series[-(n + 1) :])
    if len(tail) < n + 1 or any(x is None for x in tail):
        return None
    return all(b < a for a, b in zip(tail, tail[1:], strict=False))  # type: ignore[operator]


def _r(code: str, status: TriggerStatus, detail: str) -> TriggerResult:
    return TriggerResult(code, status, detail)


def _rule1(f: TriggerFacts) -> TriggerResult:
    c = CODES[0]
    if f.criteria is None:
        return _r(c, "not_evaluable", "no thesis")
    met = [i for i, s in f.criteria if s == "met"]
    if met:
        return _r(c, "triggered", f"criteria met: {', '.join(map(str, met))}")
    unknown = [i for i, s in f.criteria if s == "unknown"]
    if unknown:
        return _r(c, "not_evaluable", f"criteria unknown: {', '.join(map(str, unknown))}")
    return _r(c, "clear", "no kill criterion met")


def _rule2(f: TriggerFacts, n: int) -> TriggerResult:
    c = CODES[1]
    basis = f"last {n} quarters vs prior quarter; no plan figures are stored"
    rev = None if f.revenue_growth is None else declining_quarters(f.revenue_growth, n)
    mar = None if f.operating_margin is None else declining_quarters(f.operating_margin, n)
    if rev is None or mar is None:
        return _r(c, "not_evaluable", f"quarterly revenue growth or margin unavailable ({basis})")
    if rev and mar:
        return _r(c, "triggered", f"revenue growth and operating margin fell ({basis})")
    return _r(c, "clear", f"not both falling ({basis})")


def _rule3(f: TriggerFacts, cfg: ReviewSettings) -> TriggerResult:
    c = CODES[2]
    p, floor = f.valuation_percentile, cfg.valuation_percentile_min
    what = f"{cfg.valuation_metric} {cfg.valuation_history_years}y own-history percentile"
    if p is None:
        return _r(c, "not_evaluable", f"{what} unavailable")
    if p <= floor:
        return _r(c, "clear", f"{what} {p} <= {floor}")
    if f.revisions is None:
        return _r(c, "not_evaluable", f"{what} {p} > {floor}; revisions unavailable")
    if f.revisions <= 0:
        return _r(c, "triggered", f"{what} {p} > {floor} with revisions {f.revisions}")
    return _r(c, "clear", f"{what} {p} > {floor} but revisions rising")


def _rule4(f: TriggerFacts) -> TriggerResult:
    c = CODES[3]
    over = []
    if f.position_weight_pct is not None and f.position_weight_pct > f.max_position_pct:
        over.append(f"position {f.position_weight_pct}% > {f.max_position_pct}%")
    if f.sector_weight_pct is not None and f.sector_weight_pct > f.max_sector_pct:
        over.append(f"sector {f.sector_weight_pct}% > {f.max_sector_pct}%")
    if over:
        return _r(c, "triggered", "; ".join(over))
    if f.position_weight_pct is None or f.sector_weight_pct is None:
        return _r(c, "not_evaluable", "position or sector weight unavailable")
    return _r(c, "clear", "within the profile limits")


def _rule5(f: TriggerFacts, cfg: ReviewSettings, horizon: str) -> TriggerResult:
    c = CODES[4]
    if horizon != POSITIONAL:
        return _r(c, "not_applicable", "positional holdings only")
    run = None if f.closes is None else sessions_below_sma(f.closes, SMA_WINDOW)
    if run is None:
        return _r(c, "not_evaluable", f"fewer than {SMA_WINDOW} closes")
    need = cfg.below_sma200_sessions
    if run < need:
        return _r(c, "clear", f"{run} sessions below the 200-session mean (< {need})")
    if f.rs_change is None:
        return _r(c, "not_evaluable", f"{run} sessions below; relative strength unavailable")
    if f.rs_change < 0:
        return _r(c, "triggered", f"{run} sessions below with relative strength {f.rs_change}")
    return _r(c, "clear", f"{run} sessions below but relative strength {f.rs_change}")


def evaluate_triggers(
    facts: TriggerFacts, cfg: ReviewSettings, *, horizon: str
) -> tuple[TriggerResult, ...]:
    """All six PID 15.6 rules, in rule sequence."""
    return (
        _rule1(facts),
        _rule2(facts, cfg.deterioration_quarters),
        _rule3(facts, cfg),
        _rule4(facts),
        _rule5(facts, cfg, horizon),
        _r(CODES[5], "not_evaluable", "no idea run (ST-9.4)"),
    )


def action_floor(
    proposed: Action,
    criteria: Iterable[tuple[int, Status]],
    triggers: Iterable[TriggerResult],
    *,
    override_reason: str,
    review_due: bool,
    soft_reasons: Sequence[str] = (),
) -> tuple[Action, tuple[str, ...]]:
    """The final action: the proposal lowered to the cap set by criteria, rules and review date."""
    met = [i for i, s in criteria if s == "met"]
    fired = {t.code for t in triggers if t.status == "triggered"}
    notes: list[str] = []
    cap, why = 0, ""
    hard = [f"criterion {i} met" for i in met] + [c for c in HARD if c in fired]
    soft = [c for c in SOFT if c in fired] + (["review date passed"] if review_due else [])
    soft += soft_reasons
    if hard:
        lifted = override_reason.strip() != ""
        cap, why = LADDER.index("REVIEW" if lifted else "TRIM"), ", ".join(hard)
        if lifted and LADDER.index(proposed) < LADDER.index("TRIM"):
            notes.append(
                f"override_reason recorded ({override_reason.strip()}): TRIM lifted to REVIEW"
            )
    elif soft:
        cap, why = LADDER.index("REVIEW"), ", ".join(soft)
    final = LADDER[max(LADDER.index(proposed), cap)]
    if final != proposed:
        notes.insert(0, f"action lowered from {proposed} to {final}: {why}")
    return final, tuple(notes)
