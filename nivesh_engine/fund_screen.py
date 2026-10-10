"""Candidate discovery (ST-5.7): rank direct-plan schemes of one category against owner-set
constraints, with overlap against the funds already owned.

Pure and deterministic: no I/O, no clock, no model. Hard filters first (direct plan, comparable
growth option, then any requested minimum AUM, maximum TER and minimum manager tenure; an unknown
value fails a requested constraint and the reason is listed). Survivors get a weighted rank-sum
over four criteria, lower is better:

  consistency  beat_pct (higher is better), then median_excess_pct
  downside     downside capture (lower is better)
  cost         TER (lower is better)
  valuation    own-history valuation ratio (lower is better)

A scheme with no value for a criterion ranks last on it and is flagged `unavailable`, never ranked
as if it had a good value. Ties share a rank; the final tie-break is the AMFI code. The universe
is whatever the owner has loaded (held and candidate schemes), not every scheme in the market.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

from nivesh_core.config import MfScreenWeights
from nivesh_core.mf_models import FundHoldingRow
from nivesh_engine.mf_overlap import pairwise_overlap

MAX_RESULTS = 5
ZERO = Decimal(0)
SCORE = Decimal("0.0001")


@dataclass(frozen=True)
class Candidate:
    amfi_code: str
    name: str
    plan: str | None
    option: str | None
    comparable: bool = True
    aum_crore: Decimal | None = None
    ter_pct: Decimal | None = None
    tenure_years: Decimal | None = None
    beat_pct: Decimal | None = None
    median_excess_pct: Decimal | None = None
    downside_capture_pct: Decimal | None = None
    valuation_ratio: Decimal | None = None
    holdings: Sequence[FundHoldingRow] = ()


@dataclass(frozen=True)
class Constraints:
    """Requested limits; None means not requested. The plan must always be direct."""

    min_aum_crore: Decimal | None = None
    max_ter: Decimal | None = None
    min_tenure_years: Decimal | None = None


@dataclass(frozen=True)
class Rejected:
    amfi_code: str
    name: str
    reasons: list[str]


@dataclass(frozen=True)
class OverlapVsOwned:
    overlap_pct: Decimal
    with_fund: str


@dataclass(frozen=True)
class Ranked:
    amfi_code: str
    name: str
    score: Decimal
    ranks: dict[str, int]
    candidate: Candidate
    overlap: OverlapVsOwned | None
    unavailable: list[str]


@dataclass(frozen=True)
class ScreenResult:
    ranked: list[Ranked]
    rejected: list[Rejected] = field(default_factory=list)
    reason: str | None = None


def _filters(c: Candidate, k: Constraints) -> list[str]:
    why: list[str] = []
    if c.plan != "direct":
        why.append("not a direct plan" if c.plan else "plan unknown")
    if c.option == "idcw":
        why.append("IDCW option (NAV is not comparable)")
    elif not c.comparable:
        why.append("returns not comparable")
    checks: list[tuple[str, Decimal | None, Decimal | None, bool]] = [
        (
            "AUM (crore)",
            c.aum_crore,
            k.min_aum_crore,
            True,
        ),  # (label, value, limit, higher is better)
        ("TER", c.ter_pct, k.max_ter, False),
        ("manager tenure", c.tenure_years, k.min_tenure_years, True),
    ]
    for label, value, limit, higher in checks:
        if limit is None:
            continue
        if value is None:
            why.append(f"{label} unknown")
        elif (value < limit) if higher else (value > limit):
            why.append(f"{label} {value} fails the requested limit {limit}")
    return why


def _ranks(
    cands: Sequence[Candidate], key: Callable[[Candidate], tuple[Decimal, ...] | None]
) -> dict[str, int]:
    """Competition ranks (ties share the lowest rank); candidates without a key rank last."""
    keyed: list[tuple[tuple[Decimal, ...], str]] = []
    for c in cands:
        k = key(c)
        if k is not None:
            keyed.append((k, c.amfi_code))
    keyed.sort()
    out: dict[str, int] = {}
    previous: tuple[Decimal, ...] | None = None
    rank = 0
    for i, (k, code) in enumerate(keyed, start=1):
        if k != previous:
            rank, previous = i, k
        out[code] = rank
    last = len(keyed) + 1
    return {c.amfi_code: out.get(c.amfi_code, last) for c in cands}


def screen(
    candidates: Sequence[Candidate],
    constraints: Constraints,
    owned: Mapping[str, Sequence[FundHoldingRow]],
    weights: MfScreenWeights,
) -> ScreenResult:
    """Up to five ranked candidates and the rejected ones with reasons."""
    kept: list[Candidate] = []
    rejected: list[Rejected] = []
    for c in sorted(candidates, key=lambda x: x.amfi_code):
        why = _filters(c, constraints)
        if c.amfi_code in owned:
            why.append("already owned")
        if why:
            rejected.append(Rejected(c.amfi_code, c.name, why))
        else:
            kept.append(c)
    if not kept:
        reason = (
            "no candidates in the category" if not candidates else "every candidate was rejected"
        )
        return ScreenResult([], rejected, reason)
    rank_sets = {
        "consistency": _ranks(
            kept,
            lambda c: (
                None
                if c.beat_pct is None
                else (
                    -c.beat_pct,
                    -(c.median_excess_pct if c.median_excess_pct is not None else ZERO),
                )
            ),
        ),
        "downside": _ranks(
            kept, lambda c: None if c.downside_capture_pct is None else (c.downside_capture_pct,)
        ),
        "cost": _ranks(kept, lambda c: None if c.ter_pct is None else (c.ter_pct,)),
        "valuation": _ranks(
            kept, lambda c: None if c.valuation_ratio is None else (c.valuation_ratio,)
        ),
    }
    weight = {
        "consistency": weights.consistency, "downside": weights.downside, "cost": weights.cost,
        "valuation": weights.valuation,
    }  # fmt: skip
    present = {
        "consistency": lambda c: c.beat_pct is not None,
        "downside": lambda c: c.downside_capture_pct is not None,
        "cost": lambda c: c.ter_pct is not None,
        "valuation": lambda c: c.valuation_ratio is not None,
    }
    out: list[Ranked] = []
    for c in kept:
        ranks = {k: rank_sets[k][c.amfi_code] for k in rank_sets}
        score = sum((weight[k] * ranks[k] for k in ranks), ZERO).quantize(
            SCORE, rounding=ROUND_HALF_EVEN
        )
        best: OverlapVsOwned | None = None
        for code in sorted(owned) if c.holdings else []:  # no holdings: overlap unknown, not 0
            pct = pairwise_overlap(c.holdings, owned[code]).overlap_pct
            if best is None or pct > best.overlap_pct:
                best = OverlapVsOwned(pct, code)
        gone = sorted(k for k in present if not present[k](c))
        out.append(Ranked(c.amfi_code, c.name, score, ranks, c, best, gone))
    out.sort(key=lambda r: (r.score, r.amfi_code))
    return ScreenResult(out[:MAX_RESULTS], rejected)
