"""Fund analytics (ST-5.4): rolling returns, consistency against a benchmark, risk.

Pure and deterministic: Decimal only (no float anywhere), no I/O, no clock, no randomness. Math
runs under a fixed context (28 digits, ROUND_HALF_EVEN) and every output is quantised once, at the
boundary. A metric that cannot be computed is None with a reason, never zero.

Definitions (ADR-0007):
- series are ascending (date, NAV) pairs; `as_of` drops later points.
- A window ends on each NAV date `d`; its start is the latest point on or before `d - window_days`
  (no such point: the window is skipped). Fewer than 2 windows: insufficient history.
- Window return is annualised on the actual elapsed days between the start and end points:
  (end / start) ** (365 / elapsed_days) - 1, via Decimal ln and exp, reported in percent.
- Relative metrics use the dates present in both series (`coverage_pct` = aligned share of the
  fund's dates). Excess = fund window return minus benchmark window return on the same window.
- Daily return = NAV / previous NAV - 1 between consecutive points. Std dev is the sample std dev
  annualised by sqrt(252). Max drawdown is peak to trough on NAV. Downside capture is the mean fund
  return over the benchmark's down days divided by the mean benchmark return over them, in percent.
  Sortino = (252 * mean return - MAR) / (sqrt(252) * downside deviation), where downside deviation
  is the root of mean(min(r - MAR / 252, 0) ** 2) over all days.
The benchmark is a price index, not a total-return index, so excess is overstated by the dividend
yield.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from nivesh_engine import dmath

BENCHMARK_LABEL = "price index, not TRI"
IDCW_REASON = "IDCW payouts distort NAV; use the growth option"
DAYS_PER_YEAR = Decimal(365)
TRADING_DAYS = Decimal(252)
ONE, HUNDRED = Decimal(1), Decimal(100)
PCT = Decimal("0.0001")
SHARE = Decimal("0.01")
PRECISION = 28

Series = Sequence[tuple[date, Decimal]]


@dataclass(frozen=True)
class WindowStats:
    """Rolling-window consistency for one window length."""

    window_days: int
    windows: int
    median_return_pct: Decimal | None
    min_return_pct: Decimal | None
    max_return_pct: Decimal | None
    beat_pct: Decimal | None
    median_excess_pct: Decimal | None
    reason: str | None = None  # why the return statistics are unavailable
    relative_reason: str | None = None  # why beat_pct / median_excess are unavailable


@dataclass(frozen=True)
class Drawdown:
    pct: Decimal  # positive magnitude, peak to trough
    peak_date: date
    trough_date: date


@dataclass(frozen=True)
class FundAnalytics:
    comparable: bool
    reason: str | None
    benchmark_label: str
    points: int
    coverage_pct: Decimal | None
    rolling: list[WindowStats] = field(default_factory=list)
    std_dev_pct: Decimal | None = None
    std_dev_reason: str | None = None
    max_drawdown: Drawdown | None = None
    max_drawdown_reason: str | None = None
    downside_capture_pct: Decimal | None = None
    downside_capture_reason: str | None = None
    sortino: Decimal | None = None
    sortino_reason: str | None = None


def _check(name: str, series: Series) -> list[tuple[date, Decimal]]:
    out = list(series)
    for i, (d, v) in enumerate(out):
        if v <= 0:
            raise ValueError(f"{name}: value on {d} must be > 0")
        if i and d <= out[i - 1][0]:
            raise ValueError(f"{name}: dates must be strictly ascending (at {d})")
    return out


def _q(value: Decimal, quantum: Decimal = PCT) -> Decimal:
    return dmath.quantize(value, quantum)


def _windows(dates: list[date], window_days: int) -> list[tuple[int, int]]:
    out = []
    for j, d in enumerate(dates):
        i = bisect_right(dates, d - timedelta(days=window_days)) - 1
        if i >= 0:
            out.append((i, j))
    return out


def _window_returns(
    dates: list[date], values: list[Decimal], pairs: list[tuple[int, int]]
) -> list[Decimal]:
    return [dmath.cagr(values[i], values[j], (dates[j] - dates[i]).days) for i, j in pairs]


def _aligned(
    fund: list[tuple[date, Decimal]], bench: list[tuple[date, Decimal]]
) -> tuple[list[date], list[Decimal], list[Decimal]]:
    b = dict(bench)
    common = [(d, v, b[d]) for d, v in fund if d in b]
    return [c[0] for c in common], [c[1] for c in common], [c[2] for c in common]


def _daily(values: list[Decimal]) -> list[Decimal]:
    return [values[i] / values[i - 1] - ONE for i in range(1, len(values))]


def _window_stats(
    window_days: int,
    fund: list[tuple[date, Decimal]],
    aligned: tuple[list[date], list[Decimal], list[Decimal]] | None,
    relative_reason: str | None,
) -> WindowStats:
    dates = [d for d, _ in fund]
    pairs = _windows(dates, window_days)
    if len(pairs) < 2:
        why = f"insufficient history: {len(pairs)} window(s) of {window_days} days"
        return WindowStats(window_days, len(pairs), None, None, None, None, None, why, why)
    rets = _window_returns(dates, [v for _, v in fund], pairs)
    beat = excess = None
    why_rel = relative_reason
    if aligned is not None:
        a_dates, a_fund, a_bench = aligned
        a_pairs = _windows(a_dates, window_days)
        if len(a_pairs) < 2:
            why_rel = f"insufficient aligned history: {len(a_pairs)} window(s)"
        else:
            fr = _window_returns(a_dates, a_fund, a_pairs)
            br = _window_returns(a_dates, a_bench, a_pairs)
            diffs = [f - b for f, b in zip(fr, br, strict=True)]
            beat = _q(HUNDRED * sum(1 for x in diffs if x > 0) / len(diffs), SHARE)
            excess = _q(HUNDRED * dmath.median(diffs))
            why_rel = None
    return WindowStats(
        window_days, len(pairs), _q(HUNDRED * dmath.median(rets)), _q(HUNDRED * min(rets)),
        _q(HUNDRED * max(rets)), beat, excess, None, why_rel,
    )  # fmt: skip


def _std_dev(rets: list[Decimal]) -> tuple[Decimal | None, str | None]:
    if len(rets) < 2:
        return None, f"insufficient history: {len(rets)} daily return(s)"
    mean = sum(rets, Decimal(0)) / len(rets)
    var = sum(((r - mean) ** 2 for r in rets), Decimal(0)) / (len(rets) - 1)
    return _q(HUNDRED * var.sqrt() * TRADING_DAYS.sqrt()), None


def _drawdown(points: list[tuple[date, Decimal]]) -> tuple[Drawdown | None, str | None]:
    if len(points) < 2:
        return None, "insufficient history: fewer than 2 NAV points"
    peak_d, peak_v = points[0]
    worst, found = Decimal(0), (peak_d, peak_d)
    for d, v in points:
        if v > peak_v:
            peak_d, peak_v = d, v
        dd = v / peak_v - ONE
        if dd < worst:
            worst, found = dd, (peak_d, d)
    return Drawdown(_q(HUNDRED * -worst), found[0], found[1]), None


def _downside_capture(
    fund: list[Decimal], bench: list[Decimal]
) -> tuple[Decimal | None, str | None]:
    f, b = _daily(fund), _daily(bench)
    down = [(x, y) for x, y in zip(f, b, strict=True) if y < 0]
    if not down:
        return None, "no benchmark down days in the aligned history"
    fm = sum((x for x, _ in down), Decimal(0)) / len(down)
    bm = sum((y for _, y in down), Decimal(0)) / len(down)
    return _q(HUNDRED * fm / bm), None


def _sortino(rets: list[Decimal], mar_pct: Decimal) -> tuple[Decimal | None, str | None]:
    if len(rets) < 2:
        return None, f"insufficient history: {len(rets)} daily return(s)"
    mar = mar_pct / HUNDRED
    floor = mar / TRADING_DAYS
    n = len(rets)
    mean = sum(rets, Decimal(0)) / n
    downside = sum((min(r - floor, Decimal(0)) ** 2 for r in rets), Decimal(0)) / n
    dev = downside.sqrt() * TRADING_DAYS.sqrt()
    if dev == 0:
        return None, "downside deviation is zero"
    return _q((mean * TRADING_DAYS - mar) / dev), None


def analyse_fund(
    nav: Series,
    benchmark: Series | None,
    *,
    windows: Sequence[int],
    mar_pct: Decimal = Decimal(0),
    min_alignment_pct: Decimal = Decimal(80),
    option: str | None = "growth",
    as_of: date | None = None,
) -> FundAnalytics:
    """Consistency and risk metrics for one fund. Pure; same input gives byte-identical output."""
    if option == "idcw":
        return FundAnalytics(False, IDCW_REASON, BENCHMARK_LABEL, 0, None)
    fund = _check("nav", nav)
    bench = None if benchmark is None else _check("benchmark", benchmark)
    if as_of is not None:
        fund = [p for p in fund if p[0] <= as_of]
        bench = None if bench is None else [p for p in bench if p[0] <= as_of]
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = PRECISION, ROUND_HALF_EVEN
        aligned = coverage = None
        why: str | None
        if not bench:
            why = "benchmark series missing"
        elif not fund:
            why = "no NAV points"
        else:
            aligned = _aligned(fund, bench)
            coverage = _q(HUNDRED * len(aligned[0]) / len(fund), SHARE)
            why = None
            if coverage < min_alignment_pct:
                why = f"benchmark covers {coverage}% of NAV dates (minimum {min_alignment_pct}%)"
                aligned = None
        rolling = [_window_stats(w, fund, aligned, why) for w in windows]
        values = [v for _, v in fund]
        rets = _daily(values)
        sd, sd_why = _std_dev(rets)
        dd, dd_why = _drawdown(fund)
        sortino, so_why = _sortino(rets, mar_pct)
        cap, cap_why = (None, why) if aligned is None else _downside_capture(aligned[1], aligned[2])
    return FundAnalytics(
        True, None, BENCHMARK_LABEL, len(fund), coverage, rolling, sd, sd_why, dd, dd_why, cap,
        cap_why, sortino, so_why,
    )  # fmt: skip
