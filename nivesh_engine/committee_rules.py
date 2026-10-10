"""Committee safety rules (ST-7.8, ST-7.9): pure, deterministic, Decimal only, no SDK and no
model. These functions are the control: a model may propose a verdict and a weight, and the code
here lowers them. Nothing in this module can raise a verdict or clear a veto.
"""

from dataclasses import dataclass
from decimal import Decimal

HUNDRED = Decimal(100)
ZERO = Decimal(0)
LADDER: tuple[str, ...] = (
    "BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL", "AVOID", "INSUFFICIENT_DATA",
)  # fmt: skip
UPWARD: frozenset[str] = frozenset({"BUY", "ACCUMULATE"})
NO_WEIGHT: frozenset[str] = frozenset({"SELL", "AVOID", "INSUFFICIENT_DATA"})
INSUFFICIENT_BAND = "insufficient_data"
CAP_HOLD = "HOLD"


@dataclass(frozen=True)
class RiskFacts:
    """What the risk veto is decided from. Every field is computed in code, never by a model."""

    excluded: bool
    hard_flag: bool
    position_over_limit: bool
    sector_over_limit: bool
    days_to_trade: Decimal | None
    max_position_pct: Decimal
    max_sector_pct: Decimal
    sector_headroom_pct: Decimal | None
    tested_weight_pct: Decimal


@dataclass(frozen=True)
class Veto:
    veto: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Bounds:
    min_pct: Decimal
    max_pct: Decimal


def risk_veto(facts: RiskFacts, *, max_days_to_trade: Decimal | None = None) -> Veto:
    """True when any single cause applies. A limit is breached only when strictly exceeded."""
    reasons: list[str] = []
    if facts.excluded:
        reasons.append("excluded_by_profile")
    if facts.hard_flag:
        reasons.append("hard_red_flag")
    if facts.position_over_limit:
        reasons.append("position_over_limit")
    if facts.sector_over_limit:
        reasons.append("sector_over_limit")
    if (
        max_days_to_trade is not None
        and facts.days_to_trade is not None
        and facts.days_to_trade > max_days_to_trade
    ):
        reasons.append("days_to_trade_over_limit")
    return Veto(bool(reasons), tuple(reasons))


def weight_bounds(facts: RiskFacts, proposal_pct: Decimal | None = None) -> Bounds:
    """0 up to the smallest of the profile position limit, the sector headroom and the model's
    own proposal. Never negative; no headroom gives a zero upper bound."""
    top = facts.max_position_pct
    if facts.sector_headroom_pct is not None:
        top = min(top, facts.sector_headroom_pct)
    if proposal_pct is not None:
        top = min(top, proposal_pct)
    return Bounds(ZERO, max(top, ZERO))


def coverage_pct(
    inputs_available: int, inputs_total: int, views_ok: int, views_required: int
) -> Decimal:
    """The smaller of the score card's input coverage and the share of required views that came
    back valid, in percent (a missing denominator counts as zero coverage)."""
    card = HUNDRED * inputs_available / inputs_total if inputs_total > 0 else ZERO
    views = HUNDRED * views_ok / views_required if views_required > 0 else ZERO
    return min(card, views)


def below_minimum(coverage: Decimal, minimum: Decimal) -> bool:
    """Strictly below the minimum fails; exactly at it passes."""
    return coverage < minimum


@dataclass(frozen=True)
class Ceiling:
    verdict: str
    vetoed_by_risk: bool
    overrides: tuple[str, ...]


def verdict_ceiling(
    proposed: str,
    *,
    veto: bool,
    card_band: str,
    card_cap: str | None,
    hard_flag: bool,
    coverage: Decimal,
    min_coverage: Decimal,
) -> Ceiling:
    """Apply the ceilings to a proposed verdict. The result is never higher than the proposal.

    1. Insufficient data (band, or coverage strictly below the minimum) forces INSUFFICIENT_DATA.
    2. Otherwise a veto, a HOLD cap on the score card or a hard red flag lowers a top-band or
       upward verdict to HOLD. Verdicts already at HOLD or below are kept.
    Every change is listed in `overrides`."""
    if proposed not in LADDER:
        raise ValueError(f"unknown verdict {proposed!r}")
    notes: list[str] = []
    if card_band == INSUFFICIENT_BAND or below_minimum(coverage, min_coverage):
        why = (
            "score card has insufficient data"
            if card_band == INSUFFICIENT_BAND
            else (f"coverage {coverage} is below the minimum {min_coverage}")
        )
        if proposed != "INSUFFICIENT_DATA":
            notes.append(f"verdict {proposed} -> INSUFFICIENT_DATA: {why}")
        return Ceiling("INSUFFICIENT_DATA", veto, tuple(notes))
    if proposed in UPWARD:
        causes = [
            name
            for name, on in (
                ("risk veto", veto),
                ("score card cap", card_cap == CAP_HOLD),
                ("hard red flag", hard_flag),
            )
            if on
        ]
        if causes:
            notes.append(f"verdict {proposed} -> HOLD: {', '.join(causes)}")
            return Ceiling("HOLD", veto, tuple(notes))
    return Ceiling(proposed, veto, ())


def final_weight(
    verdict: str, proposal_pct: Decimal | None, bounds: Bounds, *, lowered_by_veto: bool
) -> tuple[Decimal | None, tuple[str, ...]]:
    """The suggested weight: none for the no-data, avoid and exit verdicts and for a veto-lowered
    hold, else the proposal clamped into the bounds. Clamping is listed in the second item."""
    if verdict in NO_WEIGHT or (verdict == "HOLD" and lowered_by_veto) or proposal_pct is None:
        note: tuple[str, ...] = ()
        if proposal_pct is not None:
            note = (f"suggested weight {proposal_pct} dropped for verdict {verdict}",)
        return None, note
    clamped = min(max(proposal_pct, bounds.min_pct), bounds.max_pct)
    if clamped != proposal_pct:
        return clamped, (f"suggested weight {proposal_pct} -> {clamped}: profile limits",)
    return clamped, ()
