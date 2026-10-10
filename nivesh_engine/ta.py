"""ST-6.1 technical indicators, exact decimal, from adjusted daily bars. Pure: no I/O, no clock.

Conventions (the ADR records them; tests pin them with hand-derived answers):
  SMA simple. EMA alpha 2/(N+1), seeded with the SMA of the first N closes.
  RSI(N) Wilder: average gain and loss seeded with the simple mean of the first N changes, then
  avg = (avg * (N - 1) + x) / N; a window with no price change is unavailable.
  ATR(N) Wilder, seeded with the mean of the first N true ranges (the first bar has no previous
  close, so ranges start at the second bar).
  MACD: fast and slow EMA each seeded with the SMA of their own first closes, the line starts where
  the slow EMA starts, the signal is an EMA seeded with the SMA of the first values of the line,
  the histogram is line minus signal.
  ROC is a fraction (close over close N sessions ago, minus one); names ending in _pct are percent.
  Realised volatility is the sample standard deviation of simple daily returns times sqrt(252).
  Bollinger width is (upper - lower) / middle with the population standard deviation.
  The weekly trend compares the 10 and 40 week averages of the last close of each ISO week.

An indicator that needs more bars than there are is unavailable with the reason "need N bars,
have M", never a guess. Only the last value of each indicator is materialised in `ta_compute`.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from functools import partial

from nivesh_core.analysis_config import TaSettings
from nivesh_engine import dmath
from nivesh_engine.bars import Bar, check_bars
from nivesh_engine.metric import Metric, na, ok

QUANTUM = Decimal("0.00000001")
LONG_SMA = 200
VOLUME_WINDOW = 20
WEEKLY_SHORT, WEEKLY_LONG = 10, 40
SLOPE_SMA = 50
_Z, _ONE, _HUNDRED = dmath.ZERO, dmath.ONE, dmath.HUNDRED


def sma(values: Sequence[Decimal], n: int) -> Decimal | None:
    if n <= 0 or len(values) < n:
        return None
    with localcontext(dmath.CONTEXT):
        return sum(values[-n:], _Z) / n


def sma_series(values: Sequence[Decimal], n: int) -> list[Decimal]:
    """Rolling simple average, one value per window (the first at index n - 1)."""
    if n <= 0 or len(values) < n:
        return []
    with localcontext(dmath.CONTEXT):
        total = sum(values[:n], _Z)
        out = [total / n]
        for i in range(n, len(values)):
            total += values[i] - values[i - n]
            out.append(total / n)
        return out


def ema_series(values: Sequence[Decimal], n: int) -> list[Decimal]:
    """Exponential average seeded with the SMA of the first n values; one value per index from
    n - 1 on."""
    if n <= 0 or len(values) < n:
        return []
    with localcontext(dmath.CONTEXT):
        alpha = Decimal(2) / (n + 1)
        cur = sum(values[:n], _Z) / n
        out = [cur]
        for v in values[n:]:
            cur += alpha * (v - cur)
            out.append(cur)
        return out


def ema(values: Sequence[Decimal], n: int) -> Decimal | None:
    series = ema_series(values, n)
    return series[-1] if series else None


def rsi(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Wilder RSI of the last close; None with fewer than n + 1 closes or no price change."""
    if n <= 0 or len(closes) < n + 1:
        return None
    with localcontext(dmath.CONTEXT):
        changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
        gain = sum((c for c in changes[:n] if c > 0), _Z) / n
        loss = sum((-c for c in changes[:n] if c < 0), _Z) / n
        for c in changes[n:]:
            gain = (gain * (n - 1) + (c if c > 0 else _Z)) / n
            loss = (loss * (n - 1) + (-c if c < 0 else _Z)) / n
        if gain + loss == 0:
            return None
        return _HUNDRED * gain / (gain + loss)


def true_ranges(bars: Sequence[Bar]) -> list[Decimal] | None:
    """True range of every bar after the first; None when a high or low is missing."""
    out: list[Decimal] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        if cur.high is None or cur.low is None:
            return None
        out.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return out


def atr(bars: Sequence[Bar], n: int) -> Decimal | None:
    """Wilder average true range; None with fewer than n + 1 bars or a missing high or low."""
    if n <= 0 or len(bars) < n + 1:
        return None
    ranges = true_ranges(bars)
    if ranges is None:
        return None
    with localcontext(dmath.CONTEXT):
        cur = sum(ranges[:n], _Z) / n
        for r in ranges[n:]:
            cur = (cur * (n - 1) + r) / n
        return cur


def macd(
    closes: Sequence[Decimal], fast: int, slow: int, signal: int
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(line, signal, histogram) at the last close; None until slow + signal - 1 closes exist."""
    fast_s, slow_s = ema_series(closes, fast), ema_series(closes, slow)
    if not slow_s:
        return None
    with localcontext(dmath.CONTEXT):
        line = [f - s for f, s in zip(fast_s[slow - fast :], slow_s, strict=True)]
    sig = ema_series(line, signal)
    if not sig:
        return None
    return line[-1], sig[-1], line[-1] - sig[-1]


def roc(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Close over the close n sessions ago, minus one (a fraction)."""
    if n <= 0 or len(closes) < n + 1 or closes[-1 - n] == 0:
        return None
    with localcontext(dmath.CONTEXT):
        return closes[-1] / closes[-1 - n] - _ONE


def bollinger_width(closes: Sequence[Decimal], n: int, k: Decimal) -> Decimal | None:
    """(upper - lower) / middle of the n-session band of width k population deviations."""
    if n <= 0 or len(closes) < n:
        return None
    window = closes[-n:]
    mid, sd = dmath.mean(window), dmath.stdev(window, sample=False)
    if sd is None or mid == 0:
        return None
    with localcontext(dmath.CONTEXT):
        return 2 * k * sd / mid


def simple_returns(closes: Sequence[Decimal]) -> list[Decimal] | None:
    """Simple daily returns; None when a previous close is zero."""
    if any(c == 0 for c in closes[:-1]):
        return None
    with localcontext(dmath.CONTEXT):
        return [b / a - _ONE for a, b in zip(closes, closes[1:], strict=False)]


def realised_vol(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Annualised sample deviation of the last n simple daily returns."""
    if n < 2 or len(closes) < n + 1:
        return None
    rets = simple_returns(closes[-(n + 1) :])
    sd = None if rets is None else dmath.stdev(rets)
    if sd is None:
        return None
    return sd * dmath.sqrt(dmath.TRADING_DAYS)


def updown_volume_ratio(
    closes: Sequence[Decimal], volumes: Sequence[Decimal | None], n: int
) -> Decimal | None:
    """Volume on up closes over volume on down closes across the last n sessions."""
    if n <= 0 or len(closes) < n + 1 or len(volumes) != len(closes):
        return None
    up = down = _Z
    for i in range(len(closes) - n, len(closes)):
        vol = volumes[i]
        if vol is None:
            return None
        if closes[i] > closes[i - 1]:
            up += vol
        elif closes[i] < closes[i - 1]:
            down += vol
    if down == 0:
        return None
    with localcontext(dmath.CONTEXT):
        return up / down


def high_low_distance_pct(bars: Sequence[Bar], n: int) -> tuple[Decimal, Decimal] | None:
    """(percent from the n-session high, percent from the n-session low) of the last close."""
    if n <= 0 or len(bars) < n:
        return None
    window = bars[-n:]
    highs = [b.high for b in window]
    lows = [b.low for b in window]
    if any(h is None for h in highs) or any(x is None for x in lows):
        return None
    top, bottom = max(h for h in highs if h is not None), min(x for x in lows if x is not None)
    if top == 0 or bottom == 0:
        return None
    with localcontext(dmath.CONTEXT):
        return (bars[-1].close / top - _ONE) * _HUNDRED, (bars[-1].close / bottom - _ONE) * _HUNDRED


def slope_pct(closes: Sequence[Decimal], n: int, lookback: int) -> Decimal | None:
    """Percent change of the n-session average over the last `lookback` sessions."""
    if n <= 0 or lookback <= 0 or len(closes) < n + lookback:
        return None
    now, then = sma(closes, n), sma(closes[:-lookback], n)
    if now is None or then is None or then == 0:
        return None
    with localcontext(dmath.CONTEXT):
        return (now / then - _ONE) * _HUNDRED


def weekly_closes(bars: Sequence[Bar]) -> list[Decimal]:
    """The last close of each ISO week, oldest first (the newest week may be partial)."""
    out: list[Decimal] = []
    current: tuple[int, int] | None = None
    for b in bars:
        week = b.date.isocalendar()[:2]
        if week == current:
            out[-1] = b.close
        else:
            out.append(b.close)
            current = week
    return out


def weekly_sma_pair(bars: Sequence[Bar], short: int, long: int) -> tuple[Decimal, Decimal] | None:
    weekly = weekly_closes(bars)
    s, lg = sma(weekly, short), sma(weekly, long)
    return None if s is None or lg is None else (s, lg)


def relative_ratios(stock: Sequence[Bar], other: Sequence[Bar]) -> list[Decimal]:
    """Stock close over the other series' close on the dates both have, oldest first."""
    base = {b.date: b.close for b in other}
    with localcontext(dmath.CONTEXT):
        return [b.close / base[b.date] for b in stock if base.get(b.date)]


def change_pct(ratios: Sequence[Decimal], window: int) -> Decimal | None:
    """Percent change of the last ratio over the ratio `window` observations earlier."""
    if window <= 0 or len(ratios) < window + 1 or ratios[-1 - window] == 0:
        return None
    with localcontext(dmath.CONTEXT):
        return (ratios[-1] / ratios[-1 - window] - _ONE) * _HUNDRED


def rs_percentiles(changes: Mapping[int, Decimal | None]) -> dict[int, Decimal | None]:
    """Mid-rank percentile of each security's relative-strength change within the cohort; a
    security with no change stays None and is left out of the cohort."""
    cohort = [v for v in changes.values() if v is not None]
    return {
        sid: None if v is None else dmath.percentile_rank(v, cohort) for sid, v in changes.items()
    }


@dataclass(frozen=True)
class TaResult:
    last_bar_date: date | None
    bars_used: int
    values: dict[str, Metric]


def _cell(
    have: int,
    need: int,
    compute: Callable[[], Decimal | None],
    why: str,
    **inputs: object,
) -> Metric:
    if have < need:
        return na(f"need {need} bars, have {have}")
    value = compute()
    if value is None:
        return na(why)
    return ok(dmath.quantize(value, QUANTUM), **inputs)


def _upto(series: Sequence[Bar] | None, as_of: date) -> list[Bar] | None:
    if series is None:
        return None
    check_bars(series)
    return [b for b in series if b.date <= as_of]


def _relative(
    used: Sequence[Bar], other: list[Bar] | None, label: str, window: int
) -> tuple[Metric, Metric]:
    if other is None:
        reason = f"no {label} series supplied"
        return na(reason), na(reason)
    ratios = relative_ratios(used, other)
    ratio = (
        ok(dmath.quantize(ratios[-1], QUANTUM), aligned=len(ratios))
        if ratios
        else na(f"no dates shared with the {label} series")
    )
    change = (
        ok(dmath.quantize(c, QUANTUM), aligned=len(ratios), window=window)
        if (c := change_pct(ratios, window)) is not None
        else na(f"need {window + 1} aligned bars, have {len(ratios)}")
    )
    return ratio, change


def _weekly_cell(used: Sequence[Bar], cfg: TaSettings) -> Metric:
    n = len(used)
    if n < cfg.min_bars_long:
        return na(f"need {cfg.min_bars_long} bars, have {n}")
    pair = weekly_sma_pair(used, WEEKLY_SHORT, WEEKLY_LONG)
    if pair is None:
        return na(f"need {WEEKLY_LONG} weeks, have {len(weekly_closes(used))}")
    sign = (pair[0] > pair[1]) - (pair[0] < pair[1])
    return ok(
        Decimal(sign),
        sma_short=dmath.quantize(pair[0], QUANTUM),
        sma_long=dmath.quantize(pair[1], QUANTUM),
    )


def _months(sessions: int) -> str:
    return f"{sessions // 21}m" if sessions % 21 == 0 else f"{sessions}d"


@dataclass(frozen=True)
class _Ctx:
    """The series one `ta_compute` call works on (bars up to the as-of date)."""

    used: list[Bar]
    closes: list[Decimal]
    volumes: list[Decimal | None]
    cfg: TaSettings
    bench: list[Bar] | None
    sect: list[Bar] | None

    @property
    def n(self) -> int:
        return len(self.used)

    def macd(self) -> tuple[Decimal, Decimal, Decimal] | None:
        return macd(self.closes, self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal)


def _sma_cell(c: _Ctx, w: int) -> Metric:
    return _cell(c.n, w, lambda: sma(c.closes, w), "undefined", window=w)


def _ema_cell(c: _Ctx) -> Metric:
    w = c.cfg.ema_window
    return _cell(c.n, w, lambda: ema(c.closes, w), "undefined", window=w)


def _slope_cell(c: _Ctx) -> Metric:
    lookback = c.cfg.slope_lookback
    return _cell(
        c.n,
        SLOPE_SMA + lookback,
        lambda: slope_pct(c.closes, SLOPE_SMA, lookback),
        "the earlier average is zero",
    )


def _vs_sma200_cell(c: _Ctx) -> Metric:
    def compute() -> Decimal | None:
        avg = sma(c.closes, LONG_SMA)
        return None if not avg else (c.closes[-1] / avg - _ONE) * _HUNDRED

    return _cell(c.n, LONG_SMA, compute, "the average is zero")


def _rsi_cell(c: _Ctx) -> Metric:
    p = c.cfg.rsi_period
    return _cell(c.n, p + 1, lambda: rsi(c.closes, p), "no price change in the window", period=p)


def _macd_cell(c: _Ctx, index: int) -> Metric:
    def compute() -> Decimal | None:
        got = c.macd()
        return None if got is None else got[index]

    return _cell(c.n, c.cfg.macd_slow + c.cfg.macd_signal - 1, compute, "undefined")


def _roc_cell(c: _Ctx, sessions: int) -> Metric:
    return _cell(
        c.n,
        sessions + 1,
        lambda: roc(c.closes, sessions),
        "the base close is zero",
        sessions=sessions,
    )


def _distance_cell(c: _Ctx, index: int) -> Metric:
    window = c.cfg.year_window

    def compute() -> Decimal | None:
        got = high_low_distance_pct(c.used, window)
        return None if got is None else got[index]

    return _cell(c.n, window, compute, "a high or low is missing")


def _atr_cell(c: _Ctx, as_percent: bool) -> Metric:
    p = c.cfg.atr_period

    def compute() -> Decimal | None:
        a = atr(c.used, p)
        if a is None or not as_percent:
            return a
        return None if c.closes[-1] == 0 else a / c.closes[-1] * _HUNDRED

    return _cell(c.n, p + 1, compute, "a high or low is missing")


def _vol_cell(c: _Ctx, w: int) -> Metric:
    return _cell(
        c.n, w + 1, lambda: realised_vol(c.closes, w), "a close in the window is zero", window=w
    )


def _boll_cell(c: _Ctx) -> Metric:
    k = c.cfg.bollinger_width_k
    return _cell(
        c.n,
        c.cfg.bollinger_window,
        lambda: bollinger_width(c.closes, c.cfg.bollinger_window, k),
        "the mean close is zero",
    )


def _avg_volume_cell(c: _Ctx) -> Metric:
    def compute() -> Decimal | None:
        window = c.volumes[-VOLUME_WINDOW:]
        if any(v is None for v in window):
            return None
        return dmath.mean([v for v in window if v is not None])

    return _cell(c.n, VOLUME_WINDOW, compute, "volume is missing in the window")


def _updown_cell(c: _Ctx) -> Metric:
    return _cell(
        c.n,
        VOLUME_WINDOW + 1,
        lambda: updown_volume_ratio(c.closes, c.volumes, VOLUME_WINDOW),
        "volume is missing or there is no down session in the window",
    )


def _rs_cell(c: _Ctx, label: str, index: int) -> Metric:
    other = c.bench if label == "benchmark" else c.sect
    return _relative(c.used, other, label, c.cfg.rs_window)[index]


def _cells(c: _Ctx) -> dict[str, Callable[[], Metric]]:
    cfg = c.cfg
    cells: dict[str, Callable[[], Metric]] = {}
    for w in cfg.sma_windows:
        cells[f"sma_{w}"] = partial(_sma_cell, c, w)
    cells[f"ema_{cfg.ema_window}"] = partial(_ema_cell, c)
    cells["slope_50dma_pct"] = partial(_slope_cell, c)
    cells["price_vs_sma200_pct"] = partial(_vs_sma200_cell, c)
    cells["weekly_trend"] = partial(_weekly_cell, c.used, cfg)
    cells[f"rsi_{cfg.rsi_period}"] = partial(_rsi_cell, c)
    for index, name in enumerate(("macd", "macd_signal", "macd_hist")):
        cells[name] = partial(_macd_cell, c, index)
    for s in cfg.roc_sessions:
        cells[f"roc_{_months(s)}"] = partial(_roc_cell, c, s)
    cells["dist_52w_high_pct"] = partial(_distance_cell, c, 0)
    cells["dist_52w_low_pct"] = partial(_distance_cell, c, 1)
    cells[f"atr_{cfg.atr_period}"] = partial(_atr_cell, c, False)
    cells["atr_pct"] = partial(_atr_cell, c, True)
    for w in cfg.vol_windows:
        cells[f"vol_{w}d"] = partial(_vol_cell, c, w)
    cells[f"boll_width_{cfg.bollinger_window}"] = partial(_boll_cell, c)
    cells[f"avg_volume_{VOLUME_WINDOW}"] = partial(_avg_volume_cell, c)
    cells[f"updown_volume_ratio_{VOLUME_WINDOW}"] = partial(_updown_cell, c)
    for label in ("benchmark", "sector"):
        cells[f"rs_{label}_ratio"] = partial(_rs_cell, c, label, 0)
        cells[f"rs_{label}_change_{_months(cfg.rs_window)}_pct"] = partial(_rs_cell, c, label, 1)
    return cells


def ta_compute(
    bars: Sequence[Bar],
    *,
    as_of: date,
    benchmark: Sequence[Bar] | None = None,
    sector: Sequence[Bar] | None = None,
    cfg: TaSettings | None = None,
    only: set[str] | None = None,
) -> TaResult:
    """Every ST-6.1 indicator at the last bar on or before `as_of`, as available-or-reasoned cells.

    `benchmark` and `sector` are bar series for the relative-strength cells (None: unavailable
    with a reason). `only` restricts the work to the named cells (the screener asks for what it
    needs); an unknown name is a ValueError. Unsorted or duplicated dates raise BarDataError."""
    check_bars(bars)
    used = [b for b in bars if b.date <= as_of]
    ctx = _Ctx(
        used,
        [b.close for b in used],
        [b.volume for b in used],
        cfg or TaSettings(),
        _upto(benchmark, as_of),
        _upto(sector, as_of),
    )
    cells = _cells(ctx)
    if only is not None and (unknown := only - set(cells)):
        raise ValueError(f"unknown metric(s): {', '.join(sorted(unknown))}")
    values = {k: fn() for k, fn in cells.items() if only is None or k in only}
    return TaResult(used[-1].date if used else None, len(used), values)
