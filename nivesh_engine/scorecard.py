"""Scorecard aggregation (ST-12.3) and the rule that decides which ledger rows are one call.

Pure and Decimal-only: no I/O, no clock, no randomness. The unit of count is the call, never a
score row. A call has up to five score rows (one per horizon); every dimension except `horizon`
uses only the primary horizon, and the `horizon` dimension shows each horizon as its own bucket,
so rows of one call are never pooled as if they were independent.

Mean, median and worst use `call_excess` (positive is a good call, one sign for every verdict);
HOLD calls are scored but have no hit and no call excess, so they count in `calls` and `scored`
only. A bucket with fewer than `min_bucket` scored calls is flagged `enough = False`.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, localcontext

from nivesh_engine import dmath

DIMENSIONS = ("verdict", "horizon", "market", "conviction", "command", "system_vs_acted")
_PCT_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class ScoredCall:
    """One call at one horizon. `status` is scored, insufficient_data or pending."""

    call_id: int
    verdict: str
    horizon_weeks: int
    market: str
    conviction: str
    command: str
    acted: bool
    status: str
    call_excess: Decimal | None = None
    hit: bool | None = None


@dataclass(frozen=True)
class Bucket:
    key: str
    calls: int
    scored: int
    hit_pct: Decimal | None
    mean_excess: Decimal | None
    median_excess: Decimal | None
    worst: Decimal | None
    worst_call_id: int | None
    enough: bool


@dataclass(frozen=True)
class Scorecard:
    min_bucket: int
    primary_horizon_weeks: int
    buckets: Mapping[str, tuple[Bucket, ...]] = field(default_factory=dict)


def _bucket(key: str, calls: Sequence[ScoredCall], min_bucket: int) -> Bucket:
    scored = [c for c in calls if c.status == "scored"]
    eligible = [c for c in scored if c.hit is not None]
    excess = [(c.call_excess, c.call_id) for c in scored if c.call_excess is not None]
    hit_pct = None
    if eligible:
        with localcontext(dmath.CONTEXT):
            share = Decimal(sum(1 for c in eligible if c.hit)) / len(eligible) * dmath.HUNDRED
        hit_pct = dmath.quantize(share, _PCT_QUANTUM)
    worst = min(excess, key=lambda x: (x[0], x[1])) if excess else None
    values = [v for v, _ in excess]
    return Bucket(
        key=key,
        calls=len({c.call_id for c in calls}),
        scored=len({c.call_id for c in scored}),
        hit_pct=hit_pct,
        mean_excess=dmath.quantize(dmath.mean(values)) if values else None,
        median_excess=dmath.quantize(dmath.median(values)) if values else None,
        worst=None if worst is None else worst[0],
        worst_call_id=None if worst is None else worst[1],
        enough=len({c.call_id for c in scored}) >= min_bucket,
    )


def _label(dimension: str, c: ScoredCall) -> str:
    if dimension == "horizon":
        return str(c.horizon_weeks)
    if dimension == "system_vs_acted":
        return "acted" if c.acted else "system"
    return str(getattr(c, dimension))


def _sort_key(key: str) -> tuple[int, str]:
    return (int(key), key) if key.isdigit() else (0, key)


def build_scorecard(
    calls: Sequence[ScoredCall], min_bucket: int, primary_horizon_weeks: int
) -> Scorecard:
    out: dict[str, tuple[Bucket, ...]] = {}
    for dim in DIMENSIONS:
        groups: dict[str, list[ScoredCall]] = defaultdict(list)
        for c in calls:
            if dim == "horizon" or c.horizon_weeks == primary_horizon_weeks:
                groups[_label(dim, c)].append(c)
        out[dim] = tuple(_bucket(k, groups[k], min_bucket) for k in sorted(groups, key=_sort_key))
    return Scorecard(min_bucket, primary_horizon_weeks, out)


# ---- which ledger rows are one call --------------------------------------------------------
@dataclass(frozen=True)
class CallKey:
    id: int
    security_id: int
    as_of: date
    verdict: str
    corrects_id: int | None = None


@dataclass(frozen=True)
class Dedup:
    """`survivor_of` maps every ledger id to the call it counts as (itself when it survives);
    `kept` lists the surviving ids; the counts say how many rows were folded and why."""

    survivor_of: Mapping[int, int]
    kept: tuple[int, ...]
    corrected: int
    same_day: int
    in_window: int


def dedupe_calls(calls: Sequence[CallKey], primary_horizon_weeks: int) -> Dedup:
    """Fold ledger rows into calls: a row that a later row corrects goes to its correction; rows
    with the same security, issue day and verdict keep the latest id; then repeats of one
    verdict on a security inside `7 * primary_horizon_weeks` days of a kept call fold into it
    (the earliest survives), so daily or weekly reviews do not stack."""
    by_id = {c.id: c for c in calls}
    survivor: dict[int, int] = {c.id: c.id for c in calls}
    corrected = {c.corrects_id: c.id for c in calls if c.corrects_id in by_id}
    for old, new in corrected.items():
        survivor[old] = new
    live = [c for c in sorted(calls, key=lambda c: c.id) if c.id not in corrected]
    same: dict[tuple[int, date, str], int] = {}
    same_day = 0
    for c in live:  # ids ascend, so the last write is the latest id
        k = (c.security_id, c.as_of, c.verdict)
        if k in same:
            survivor[same[k]] = c.id
            same_day += 1
        same[k] = c.id
    firsts = [by_id[i] for i in same.values()]
    window = timedelta(days=7 * primary_horizon_weeks)
    kept: list[int] = []
    in_window = 0
    last_kept: dict[tuple[int, str], CallKey] = {}
    for c in sorted(firsts, key=lambda c: (c.as_of, c.id)):
        pair = (c.security_id, c.verdict)
        head = last_kept.get(pair)
        if head is not None and c.as_of < head.as_of + window:
            survivor[c.id] = head.id
            in_window += 1
        else:
            last_kept[pair] = c
            kept.append(c.id)
    resolved = {i: _resolve(i, survivor) for i in survivor}
    return Dedup(resolved, tuple(sorted(kept)), len(corrected), same_day, in_window)


def _resolve(i: int, survivor: Mapping[int, int]) -> int:
    while survivor[i] != i:
        i = survivor[i]
    return i
