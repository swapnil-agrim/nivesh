"""When something is worth an alert (ST-11.4, ST-11.6) and how many alerts reach the owner
(ST-11.7). Pure and Decimal-only: prices, volumes and dates come in as arguments.

A rule returns a `Hit` with one plain-language detail made of percentages and price levels only.
Hygiene decides which hits are sent: the same rule on the same security at most once per
`dedupe_sessions` sessions of that security's market, no more than `daily_cap` per IST day, and
a digest when more than `digest_threshold` would go out inside one rolling hour.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from nivesh_core.errors import CalendarUnknown
from nivesh_core.mf_models import FundMeta
from nivesh_core.schedule_config import AlertSettings
from nivesh_core.timeutil import ist_date
from nivesh_engine import dmath
from nivesh_engine.bars import Bar
from nivesh_engine.calendar import Calendar
from nivesh_engine.ta import simple_returns

BIG_MOVE_CLOSES = 22  # today's close, the one before it, and 20 earlier daily returns
WEEK52_SESSIONS = 252
VOLUME_DAYS = 20
HUNDRED = Decimal(100)
CENT = Decimal("0.01")
TENTH = Decimal("0.1")
WEEKDAY_FALLBACK = "weekday fallback"
MERGE_WORD = re.compile(r"\bmerg")  # merger, merged, merging; not "emerging"
Held = Literal["dedupe", "cap"]


@dataclass(frozen=True)
class Hit:
    rule: str
    detail: str
    levels: tuple[str, ...] = ()  # price levels the detail mentions, as text


def _pct(x: Decimal) -> Decimal:
    return (x * HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def _level(x: Decimal) -> str:
    return format(x.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def big_move(closes: Sequence[Decimal], mult: Decimal) -> Hit | None:
    """The last daily move is larger than `mult` times the spread of the 20 daily moves before
    it. `closes` is oldest first and needs 22 values; the last move is left out of its own
    baseline, so a spike cannot raise the bar it is judged by."""
    if len(closes) < BIG_MOVE_CLOSES or closes[-2] == 0:
        return None
    base = simple_returns(closes[-BIG_MOVE_CLOSES:-1])
    spread = None if base is None else dmath.stdev(base)
    if spread is None or spread == 0:
        return None
    move = closes[-1] / closes[-2] - 1
    if abs(move) <= mult * spread:
        return None
    times = (abs(move) / spread).quantize(TENTH, rounding=ROUND_HALF_UP)
    sign = "+" if move > 0 else ""
    return Hit(
        "big_move", f"closed {sign}{_pct(move)}% on the day, {times} times its usual daily move"
    )


def below_invalidation(close: Decimal, level: Decimal) -> Hit | None:
    if close >= level:
        return None
    return Hit(
        "close_below_invalidation",
        f"closed at {_level(close)}, below your invalidation level {_level(level)}",
        (_level(close), _level(level)),
    )


def entered_zone(
    prev_close: Decimal, close: Decimal, low: Decimal | None, high: Decimal | None
) -> Hit | None:
    """The close is inside the entry zone and the one before it was outside (entry, not stay)."""
    if low is None or high is None:
        return None
    if low <= prev_close <= high or not (low <= close <= high):
        return None
    return Hit(
        "watch_zone_entry",
        f"closed at {_level(close)}, inside your entry zone {_level(low)} to {_level(high)}",
        (_level(close), _level(low), _level(high)),
    )


def break_52w(bars: Sequence[Bar], vol_mult: Decimal) -> Hit | None:
    """A close beyond the high or low of the previous 252 sessions on volume above `vol_mult`
    times the 20-session average."""
    if len(bars) < WEEK52_SESSIONS + 1:
        return None
    prior, last = bars[-(WEEK52_SESSIONS + 1) : -1], bars[-1]
    vols = [b.volume for b in prior[-VOLUME_DAYS:] if b.volume is not None]
    if last.volume is None or len(vols) < VOLUME_DAYS:
        return None
    avg = dmath.mean(vols)
    if avg <= 0 or last.volume <= vol_mult * avg:
        return None
    top = max(b.high if b.high is not None else b.close for b in prior)
    bottom = min(b.low if b.low is not None else b.close for b in prior)
    times = (last.volume / avg).quantize(TENTH, rounding=ROUND_HALF_UP)
    if last.close > top:
        way, mark = "above", top
    elif last.close < bottom:
        way, mark = "below", bottom
    else:
        return None
    return Hit(
        "break_52w",
        f"closed at {_level(last.close)}, {way} its 52-week range edge {_level(mark)}, "
        f"on volume {times} times the 20-day average",
        (_level(last.close), _level(mark)),
    )


def mf_changes(prev: FundMeta | None, cur: FundMeta, ter_pp: Decimal) -> list[Hit]:
    """What changed between two metadata rows of an owned fund. Merger is a proxy: a changed
    scheme code or a name or source that says so; it is labelled best effort."""
    if prev is None:
        return []
    out: list[Hit] = []
    if prev.manager != cur.manager and cur.manager is not None:
        out.append(Hit("mf_manager_change", "the fund manager changed"))
    if prev.category != cur.category and cur.category is not None:
        out.append(Hit("mf_category_change", f"the category changed to {cur.category}"))
    if prev.expense_ratio is not None and cur.expense_ratio is not None:
        delta = cur.expense_ratio - prev.expense_ratio
        if abs(delta) > ter_pp:
            sign = "+" if delta > 0 else ""
            out.append(
                Hit("mf_ter_change", f"the expense ratio moved {sign}{delta.quantize(CENT)} points")
            )
    text = f"{cur.scheme_name} {cur.source}".lower()
    if prev.amfi_code != cur.amfi_code or MERGE_WORD.search(text):
        out.append(Hit("mf_merger", "possible merger (best effort)"))
    return out


# ---- hygiene -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Pending:
    """An alert that has fired and is waiting for the hygiene decision."""

    rule: str
    security_id: int | None
    market: str  # IN or US: whose sessions count for the repeat window
    trading_date: date


@dataclass(frozen=True)
class Past:
    """An alert that went out earlier (alone or in a digest)."""

    rule: str
    security_id: int | None
    trading_date: date
    fired_at: datetime


@dataclass(frozen=True)
class Plan:
    send: tuple[int, ...]  # indexes into the pending list, to send one by one
    digest: tuple[int, ...]  # indexes to send as a single digest
    held: tuple[tuple[int, Held], ...]  # indexes kept back, with the reason
    notes: tuple[tuple[int, str], ...] = ()  # e.g. a weekday fallback


def _sessions_after(cal: Calendar, start: date, end: date) -> tuple[int, bool]:
    """(sessions in the days after `start` up to `end`, whether weekdays had to stand in)."""
    first = start + timedelta(days=1)
    if end < first:
        return 0, False
    try:
        return len(cal.sessions(first, end)), False
    except CalendarUnknown:
        days = (first + timedelta(days=i) for i in range((end - first).days + 1))
        return sum(1 for d in days if d.weekday() < 5), True


def hygiene(
    fired: Sequence[Pending], history: Sequence[Past], calendars: Mapping[str, Calendar],
    as_at: datetime, cfg: AlertSettings,
) -> Plan:  # fmt: skip
    """Decide what is sent. `history` holds only alerts that went out; held-back ones never
    count, so a held alert does not push the next one away. When the daily cap bites, the
    sequence of `fired` is the sequence of importance."""
    sent_by_key: dict[tuple[str, int | None], list[date]] = {}
    for p in history:
        sent_by_key.setdefault((p.rule, p.security_id), []).append(p.trading_date)
    held: list[tuple[int, Held]] = []
    notes: list[tuple[int, str]] = []
    live: list[int] = []
    for i, f in enumerate(fired):
        last = max(sent_by_key.get((f.rule, f.security_id), []), default=None)
        if last is not None:
            n, weekdays = _sessions_after(calendars[f.market], last, f.trading_date)
            if weekdays:
                notes.append((i, WEEKDAY_FALLBACK))
            if n <= cfg.dedupe_sessions:
                held.append((i, "dedupe"))
                continue
        live.append(i)
        sent_by_key.setdefault((f.rule, f.security_id), []).append(f.trading_date)
    today = ist_date(as_at)
    used = sum(1 for p in history if ist_date(p.fired_at) == today)
    room = max(cfg.daily_cap - used, 0)
    held += [(i, "cap") for i in live[room:]]
    live = live[:room]
    window = as_at - timedelta(minutes=cfg.digest_window_min)
    recent = sum(1 for p in history if window < p.fired_at <= as_at)
    if len(live) > 1 and recent + len(live) > cfg.digest_threshold:
        return Plan((), tuple(live), tuple(held), tuple(notes))
    return Plan(tuple(live), (), tuple(held), tuple(notes))
