"""Shared exact-decimal math for the analysis engines (ST-6.x) and the fund analytics (ST-5.4).

Pure and Decimal-only: no I/O, no clock, no randomness, no binary floating point. Every function
runs under one fixed context (28 digits, ROUND_HALF_EVEN), so results do not depend on the caller's
context. A statistic that is not defined for its input (too few points, zero variance) is None, a
caller error (empty input, unequal lengths) is a ValueError. Outputs are quantised once, by the
caller, at the boundary with `quantize`.
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
ZERO, ONE, HUNDRED = Decimal(0), Decimal(1), Decimal(100)
DAYS_PER_YEAR = Decimal(365)
TRADING_DAYS = Decimal(252)
DEFAULT_QUANTUM = Decimal("0.0001")


def quantize(value: Decimal, quantum: Decimal = DEFAULT_QUANTUM) -> Decimal:
    """Round half to even to the exponent of `quantum` (the one rounding at the boundary)."""
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean of an empty sequence")
    with localcontext(CONTEXT):
        return sum(values, ZERO) / len(values)


def median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("median of an empty sequence")
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    with localcontext(CONTEXT):
        return (s[mid - 1] + s[mid]) / 2


def sqrt(value: Decimal) -> Decimal:
    with localcontext(CONTEXT):
        return value.sqrt()


def ln(value: Decimal) -> Decimal:
    with localcontext(CONTEXT):
        return value.ln()


def exp(value: Decimal) -> Decimal:
    with localcontext(CONTEXT):
        return value.exp()


def cagr(start: Decimal, end: Decimal, elapsed_days: int) -> Decimal:
    """Annualised fractional return over `elapsed_days` (actual/365), via ln and exp."""
    with localcontext(CONTEXT):
        return ((end / start).ln() * DAYS_PER_YEAR / elapsed_days).exp() - ONE


def stdev(values: Sequence[Decimal], *, sample: bool = True) -> Decimal | None:
    """Sample (n - 1) or population (n) standard deviation; None with too few points."""
    n = len(values)
    if n < (2 if sample else 1):
        return None
    with localcontext(CONTEXT):
        m = sum(values, ZERO) / n
        var = sum(((v - m) ** 2 for v in values), ZERO) / (n - 1 if sample else n)
        return var.sqrt()


def percentile_rank(value: Decimal, cohort: Sequence[Decimal]) -> Decimal | None:
    """Mid-rank percentile in [0, 100]: (strictly below + half of equal) / n. A cohort of one
    scores 50, never 100. None for an empty cohort."""
    n = len(cohort)
    if n == 0:
        return None
    below = sum(1 for c in cohort if c < value)
    equal = sum(1 for c in cohort if c == value)
    with localcontext(CONTEXT):
        return HUNDRED * (Decimal(below) + Decimal(equal) / 2) / n


def covariance(
    xs: Sequence[Decimal], ys: Sequence[Decimal], *, sample: bool = True
) -> Decimal | None:
    if len(xs) != len(ys):
        raise ValueError("series must have equal length")
    n = len(xs)
    if n < (2 if sample else 1):
        return None
    with localcontext(CONTEXT):
        mx, my = sum(xs, ZERO) / n, sum(ys, ZERO) / n
        total = sum(((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)), ZERO)
        return total / (n - 1 if sample else n)


def correlation(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal | None:
    """Pearson correlation; None when either series has no variance or fewer than 2 points."""
    cov, sx, sy = covariance(xs, ys), stdev(xs), stdev(ys)
    if cov is None or not sx or not sy:
        return None
    with localcontext(CONTEXT):
        return cov / (sx * sy)


def beta(xs: Sequence[Decimal], benchmark: Sequence[Decimal]) -> Decimal | None:
    """Slope of `xs` on `benchmark`: cov(x, b) / var(b); None when the benchmark is flat."""
    cov, sb = covariance(xs, benchmark), stdev(benchmark)
    if cov is None or not sb:
        return None
    with localcontext(CONTEXT):
        return cov / (sb * sb)
