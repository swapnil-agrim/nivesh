"""Calibration of conviction (ST-12.4, OBJ-8): does a higher conviction hit more often?

Pure and Decimal-only: no I/O, no clock. The confidence intervals come from a seeded bootstrap
driven by an integer xorshift generator, so a report is identical on every run for the same
`resample_seed`. The start state must not be zero (the generator would never leave it).

The input is one entry per call at the primary horizon (the loader has already folded repeats),
never one per score row. A call is hit-eligible when it is scored and not a HOLD. Below
`calibration_min` eligible calls there is no report: "not enough data". Monotonic means the hit
rate does not fall from high to medium to low conviction (non-strict); the verdict is
`inconclusive` when the intervals that would decide it overlap.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from nivesh_core.errors import NiveshError
from nivesh_engine import dmath

_MASK = (1 << 64) - 1
LEVELS = ("high", "medium", "low")
NOT_ENOUGH = "not enough data"
_PCT = Decimal("0.01")


def xorshift(rng_state: int) -> int:
    """The next 64-bit state of a xorshift generator (13, 7, 17)."""
    x = rng_state & _MASK
    x ^= (x << 13) & _MASK
    x ^= x >> 7
    x ^= (x << 17) & _MASK
    return x


def bootstrap_ci(
    values: Sequence[Decimal], resamples: int, resample_seed: int, level: int = 90
) -> tuple[Decimal, Decimal]:
    """Percentile interval of the mean of `values` over `resamples` draws with replacement.
    `level` is a whole percent (90 gives the 5th and 95th percentile)."""
    if resample_seed <= 0:
        raise NiveshError("resample_seed must be a positive integer")
    if not values:
        raise NiveshError("a bootstrap needs at least one value")
    if resamples < 1 or not 0 < level < 100:
        raise NiveshError("resamples must be 1 or more and level between 1 and 99")
    n = len(values)
    state = resample_seed
    means: list[Decimal] = []
    with localcontext(dmath.CONTEXT):
        for _ in range(resamples):
            total = dmath.ZERO
            for _ in range(n):
                state = xorshift(state)
                total += values[state % n]
            means.append(total / n)
    means.sort()
    low = (100 - level) * resamples // 200
    high = ((100 + level) * resamples + 199) // 200 - 1
    return means[low], means[min(high, resamples - 1)]


@dataclass(frozen=True)
class CalCall:
    """One call at the primary horizon: its conviction and, when scored, its hit and excess."""

    call_id: int
    conviction: str
    hit: bool | None
    call_excess: Decimal | None


@dataclass(frozen=True)
class CalBucket:
    conviction: str
    n: int
    hit_pct: Decimal | None
    hit_ci: tuple[Decimal, Decimal] | None
    mean_excess: Decimal | None
    excess_ci: tuple[Decimal, Decimal] | None


@dataclass(frozen=True)
class Calibration:
    n: int  # hit-eligible calls
    calibration_min: int
    enough: bool
    buckets: tuple[CalBucket, ...]
    verdict: str


def _pct(v: Decimal) -> Decimal:
    return dmath.quantize(v, _PCT)


def _bucket(level: str, calls: Sequence[CalCall], resamples: int, seed: int) -> CalBucket:
    if not calls:
        return CalBucket(level, 0, None, None, None, None)
    hits = [dmath.HUNDRED if c.hit else dmath.ZERO for c in calls]
    excess = [c.call_excess for c in calls if c.call_excess is not None]
    lo, hi = bootstrap_ci(hits, resamples, seed)
    e_ci = None
    if excess:
        a, b = bootstrap_ci(excess, resamples, seed)
        e_ci = (dmath.quantize(a), dmath.quantize(b))
    return CalBucket(
        level, len(calls), _pct(dmath.mean(hits)), (_pct(lo), _pct(hi)),
        dmath.quantize(dmath.mean(excess)) if excess else None, e_ci,
    )  # fmt: skip


def _overlap(a: CalBucket, b: CalBucket) -> bool:
    a_lo, a_hi = a.hit_ci or (dmath.ZERO, dmath.ZERO)  # buckets compared have calls
    b_lo, b_hi = b.hit_ci or (dmath.ZERO, dmath.ZERO)
    return a_lo <= b_hi and b_lo <= a_hi


def _verdict(buckets: Sequence[CalBucket]) -> str:
    have = [b for b in buckets if b.n > 0]
    if len(have) < 2:
        return "inconclusive"
    pairs = list(zip(have, have[1:], strict=False))
    reversed_pairs = [(a, b) for a, b in pairs if (a.hit_pct or 0) < (b.hit_pct or 0)]
    if reversed_pairs:
        # a fall in the wrong direction counts only when its intervals are apart
        apart = any(not _overlap(a, b) for a, b in reversed_pairs)
        return "not monotonic" if apart else "inconclusive"
    return "inconclusive" if any(_overlap(a, b) for a, b in pairs) else "monotonic"


def calibrate(
    calls: Sequence[CalCall], calibration_min: int, resamples: int, resample_seed: int
) -> Calibration:
    """Hit rate and mean excess per conviction bucket with bootstrap intervals, and a verdict."""
    eligible = [c for c in calls if c.hit is not None]
    if resample_seed <= 0:
        raise NiveshError("resample_seed must be a positive integer")
    if len(eligible) < calibration_min:
        return Calibration(len(eligible), calibration_min, False, (), NOT_ENOUGH)
    buckets = tuple(
        _bucket(lv, [c for c in eligible if c.conviction == lv], resamples, resample_seed)
        for lv in LEVELS
    )
    return Calibration(len(eligible), calibration_min, True, buckets, _verdict(buckets))
