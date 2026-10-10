"""Asset-class rebalancing proposal (ST-8.4): moves that bring each class back inside its band.

Pure and Decimal-only: no I/O, no clock. A class is out of band when its weight differs from the
target by more than `band_pp` percentage points (exactly the band is inside). The sequence is fixed:
(1) new cash goes to under-weight classes in proportion to their shortfall to target; (2) each
over-weight class is reduced only to its band edge, first from holdings a review flagged EXIT,
then TRIM, then the rest by lowest estimated tax per rupee (unknown tax last, noted); (3) the
proceeds go to under-weight classes. Turnover is the sum of reductions over the portfolio value
before the proposal; the proposal stops at `turnover_limit_pct` (a partial last move) and says
what stays out of band. A class with no target (e.g. unclassified) is left as is, with a note.
Proposal objects only: nothing here acts on a holding.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Literal

ZERO, HUNDRED, CENT = Decimal(0), Decimal(100), Decimal("0.01")
Kind = Literal["add", "reduce"]
FLAG_RANK = {"EXIT": 0, "TRIM": 1}


@dataclass(frozen=True)
class Position:
    key: str
    name: str
    asset_class: str
    value_inr: Decimal
    flag: str | None = None  # the review action (EXIT, TRIM, ...) when a review run is given
    tax_per_inr: Decimal | None = None  # estimated tax per rupee reduced; None when unknown


@dataclass(frozen=True)
class Move:
    kind: Kind
    asset_class: str
    key: str  # key and name are "" for a class-level add
    name: str
    amount_inr: Decimal
    basis: str


@dataclass(frozen=True)
class ClassLine:
    asset_class: str
    before_pct: Decimal
    target_pct: Decimal
    after_pct: Decimal
    after_inr: Decimal
    within_band: bool


@dataclass(frozen=True)
class RebalancePlan:
    moves: tuple[Move, ...] = ()
    pro_forma: tuple[ClassLine, ...] = ()
    turnover_pct: Decimal = ZERO
    within_band: bool = True
    notes: tuple[str, ...] = ()
    reason: str | None = None


def _down(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_DOWN)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    return (HUNDRED * part / total).quantize(CENT, rounding=ROUND_HALF_EVEN) if total else ZERO


def _spread(amount: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """`amount` split in proportion to positive `weights` (paise; the largest takes the rest)."""
    live = sorted((w, c) for c, w in weights.items() if w > 0)
    whole = sum((w for w, _ in live), ZERO)
    if not live or amount <= 0:
        return {}
    out = {c: _down(amount * w / whole) for w, c in live[:-1]}
    out[live[-1][1]] = amount - sum(out.values(), ZERO)
    return {c: a for c, a in out.items() if a > 0}


def _basis(p: Position) -> str:
    if p.flag in FLAG_RANK:
        return f"review {p.flag}"
    return "lowest tax per INR" if p.tax_per_inr is not None else "tax unknown"


def _rank(p: Position) -> tuple[int, Decimal, str]:
    tier = FLAG_RANK.get(p.flag or "", 2 if p.tax_per_inr is not None else 3)
    return tier, p.tax_per_inr if tier == 2 and p.tax_per_inr is not None else ZERO, p.key


def propose_moves(
    targets: Mapping[str, Decimal], positions: Sequence[Position], *, band_pp: Decimal,
    new_cash: Decimal = ZERO, turnover_limit_pct: Decimal,
) -> RebalancePlan:  # fmt: skip
    """Moves (add per class, reduce per holding), pro-forma weights and turnover in percent."""
    total = sum((p.value_inr for p in positions), ZERO)
    if not targets:
        return RebalancePlan(reason="no target allocation in the profile")
    if total <= 0:
        return RebalancePlan(reason="no valued holdings")
    if new_cash < 0:
        return RebalancePlan(reason="new cash must not be negative")
    classes = sorted(set(targets) | {p.asset_class for p in positions})
    tgt = {c: targets.get(c, ZERO) for c in classes}
    value = {c: sum((p.value_inr for p in positions if p.asset_class == c), ZERO) for c in classes}
    before = {c: _pct(value[c], total) for c in classes}
    grand = total + new_cash
    moves: list[Move] = []
    untargeted = [c for c in classes if c not in targets]
    notes = [f"no target for {', '.join(untargeted)}: left as is"] if untargeted else []

    def shortfall(c: str) -> Decimal:
        return max(tgt[c] * grand / HUNDRED - value[c], ZERO)

    def add(amounts: Mapping[str, Decimal], basis: str) -> None:
        for c in sorted(amounts):
            value[c] += amounts[c]
            moves.append(Move("add", c, "", "", amounts[c], basis))

    if new_cash > 0:
        cash = _spread(new_cash, {c: shortfall(c) for c in classes})
        rest = new_cash - sum(cash.values(), ZERO)
        for c, a in _spread(rest, tgt).items():
            cash[c] = cash.get(c, ZERO) + a
        add(cash, "new cash")

    limit = turnover_limit_pct * total / HUNDRED
    used = ZERO
    over = {c: value[c] - (tgt[c] + band_pp) * grand / HUNDRED for c in classes}
    reducible = (c for c in classes if over[c] > 0 and c in targets)
    for c in sorted(reducible, key=lambda c: (-over[c], c)):
        excess = over[c]
        unknown: list[str] = []
        for p in sorted((p for p in positions if p.asset_class == c), key=_rank):
            if limit - used <= 0 or excess <= 0:
                break
            amount = _down(min(p.value_inr, excess, limit - used))
            if amount <= 0:
                continue
            moves.append(Move("reduce", c, p.key, p.name, amount, _basis(p)))
            unknown += [p.name] if _basis(p) == "tax unknown" else []
            used += amount
            excess -= amount
            value[c] -= amount
        if unknown:
            notes.append(f"tax per INR unknown, ranked last: {', '.join(unknown)}")
    if used > 0:
        reduced = {m.asset_class for m in moves if m.kind == "reduce"}
        proceeds = _spread(used, {c: shortfall(c) for c in classes if c not in reduced})
        add(proceeds, "proceeds")
        left = used - sum(proceeds.values(), ZERO)
        if left > 0:
            notes.append(f"proceeds not placed (no class below target): {left}")
    lines = tuple(
        ClassLine(c, before[c], tgt[c], _pct(value[c], grand), value[c],
                  abs(_pct(value[c], grand) - tgt[c]) <= band_pp)
        for c in classes
    )  # fmt: skip
    out = [x.asset_class for x in lines if not x.within_band and x.asset_class in targets]
    if limit - used < CENT and out:
        notes.append(
            f"turnover cap {turnover_limit_pct}% reached; still out of band: {', '.join(out)}"
        )
    elif out:
        notes.append(f"still out of band: {', '.join(out)}")
    notes.append("per-proposal cap; annual turnover not tracked yet")
    return RebalancePlan(tuple(moves), lines, _pct(used, total), not out, tuple(notes))
