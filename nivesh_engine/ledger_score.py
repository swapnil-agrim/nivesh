"""Scoring of one ledger call at one horizon (ST-12.2).

Pure and Decimal-only: no I/O, no clock (the caller passes `today`), no randomness. Returns are
split-adjusted price returns in percent against a price index, not a total-return index; both
legs are read from the same series, so a later split cannot change a stored return.

Dates are bar dates of each series (no holiday calendar). The entry bar is the bar dated
`price_date` when the call has one, else the last bar on or before the issue day (within
`ENTRY_SNAP_DAYS`), never a later bar. The end bar is the last bar on or before the target date
and is never beyond the latest stored bar. A call stays pending (None) until both series have a
bar on or after the target; nothing is carried forward. After `stale_days` of grace a call that
still cannot be measured is recorded as insufficient data, which is counted but not scored.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from nivesh_engine import dmath

ENTRY_SNAP_DAYS = 5
_GAIN_SIDE = frozenset({"BUY", "ACCUMULATE"})
_AVOID_SIDE = frozenset({"SELL", "TRIM", "AVOID"})
_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class Series:
    """Closes by bar date, oldest first."""

    dates: tuple[date, ...]
    closes: tuple[Decimal, ...]

    @staticmethod
    def of(pairs: Sequence[tuple[date, Decimal]]) -> "Series":
        ordered = sorted(pairs, key=lambda p: p[0])
        return Series(tuple(d for d, _ in ordered), tuple(c for _, c in ordered))

    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None

    def on_or_before(self, day: date) -> tuple[date, Decimal] | None:
        i = bisect_right(self.dates, day)
        return None if i == 0 else (self.dates[i - 1], self.closes[i - 1])

    def at(self, day: date) -> Decimal | None:
        found = self.on_or_before(day)
        return found[1] if found is not None and found[0] == day else None


@dataclass(frozen=True)
class ScoreResult:
    status: Literal["scored", "insufficient_data"]
    ret: Decimal | None = None
    benchmark_ret: Decimal | None = None
    cost_pct: Decimal | None = None
    excess: Decimal | None = None  # ret - benchmark_ret, for display only
    call_excess: Decimal | None = None  # positive is a good call; None for HOLD
    hit: bool | None = None
    end_date: date | None = None
    reason: str = ""


def target_date(as_of: date, horizon_weeks: int) -> date:
    return as_of + timedelta(days=7 * horizon_weeks)


def _entry(series: Series, day: date, price_date: date | None) -> tuple[date, Decimal] | None:
    if price_date is not None:
        close = series.at(price_date)
        return None if close is None else (price_date, close)
    found = series.on_or_before(day)
    if found is None or (day - found[0]).days > ENTRY_SNAP_DAYS:
        return None
    return found


def _pct(end: Decimal, start: Decimal) -> Decimal:
    with localcontext(dmath.CONTEXT):
        return dmath.quantize((end / start - 1) * dmath.HUNDRED, _QUANTUM)


def score_call(
    verdict: str, as_of: date, price_date: date | None, security: Series, benchmark: Series,
    horizon_weeks: int, cost_pct: Decimal, today: date, stale_days: int,
) -> ScoreResult | None:  # fmt: skip
    """None while the call is not yet measurable; otherwise a scored or insufficient result."""
    if verdict == "INSUFFICIENT_DATA":
        return ScoreResult("insufficient_data", reason="the call itself had insufficient data")
    target = target_date(as_of, horizon_weeks)

    def unscorable(why: str) -> ScoreResult | None:
        if today >= target + timedelta(days=stale_days):
            return ScoreResult("insufficient_data", reason=why)
        return None

    entry = _entry(security, as_of, price_date)
    if entry is None:
        return unscorable("no entry bar for the security")
    bench_entry = benchmark.on_or_before(entry[0])
    if bench_entry is None or (entry[0] - bench_entry[0]).days > ENTRY_SNAP_DAYS:
        return unscorable("no entry bar for the benchmark")
    sec_last, bench_last = security.last_date(), benchmark.last_date()
    if sec_last is None or bench_last is None or sec_last < target or bench_last < target:
        return unscorable("a series has no bar on or after the target date")
    # both entry bars fall on or before the target, so these always find a bar
    sec_end = security.on_or_before(target) or entry
    bench_end = benchmark.on_or_before(target) or bench_entry
    if (target - sec_end[0]).days > stale_days or (target - bench_end[0]).days > stale_days:
        return unscorable("the last bar before the target date is stale")
    if entry[1] <= 0 or bench_entry[1] <= 0:
        return unscorable("the entry close is not positive")
    ret, bench = _pct(sec_end[1], entry[1]), _pct(bench_end[1], bench_entry[1])
    excess = ret - bench
    cost = cost_pct if verdict in _GAIN_SIDE else dmath.ZERO
    call_excess: Decimal | None = None
    if verdict in _GAIN_SIDE:
        call_excess = excess - cost
    elif verdict in _AVOID_SIDE:
        call_excess = bench - ret
    return ScoreResult(
        "scored", ret, bench, cost, excess, call_excess,
        None if call_excess is None else call_excess > 0, sec_end[0],
    )  # fmt: skip
