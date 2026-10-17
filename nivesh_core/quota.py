"""Vendor call ledger and quota gate (ADR-0016).

A `Budget` reserves one ledger row BEFORE a request leaves the machine (so a crash can only
over-count) and refuses the call that would pass a cap. Every attempt counts and nothing is ever
refunded: a row's accounting columns are fixed at insert and only its outcome is set, once (the
migration enforces it). The caps are the owner's belief about the vendor plan, not something read
from the vendor, so `set_limit` overrides them per field and `add_manual` records calls that other
clients of the same plan spent. Windows are the IST day and an IST month that starts on
`month_start_day`; times are UTC ISO text and `day_ist` is the IST date as ISO text.

The connection must be in autocommit mode (`isolation_level=None`), as everywhere else here.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from nivesh_core.errors import NiveshError, QuotaExceeded
from nivesh_core.fundamentals_config import PlanSettings
from nivesh_core.timeutil import ist_date, to_iso, utcnow

# The ledger is append-only, so a typo cannot be taken back; one count or limit above this is
# refused (also keeps SQLite's 64-bit SUM far from overflow, which would fail every reserve closed).
MAX_CALLS = 1_000_000
WARN_PCT = 80  # a window at or past this share of its cap is listed in QuotaStatus.warnings
Priority = Literal["holdings", "watchlist", "universe"]
Outcome = Literal["ok", "error", "halted"]
WindowName = Literal["day", "month"]
_ADJ = {"day": "daily", "month": "monthly"}


@dataclass(frozen=True)
class WindowUse:
    name: WindowName
    used: int  # every ledger row of the window, manual ones included
    manual: int  # of which entered by hand
    limit: int | None  # None: the plan has no such cap
    source: str  # "config" or "override"
    starts_on: date  # first IST day of the window
    resets_on: date  # first IST day of the next window

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)

    @property
    def resets(self) -> str:
        return "00:00 IST" if self.name == "day" else f"00:00 IST on {self.resets_on.isoformat()}"


@dataclass(frozen=True)
class QuotaStatus:
    vendor: str
    day: WindowUse
    month: WindowUse
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Limits:
    daily: int | None
    monthly: int | None
    daily_source: str
    monthly_source: str


def _month_window(today: date, start_day: int) -> tuple[date, date]:
    """(first day, first day of the next window) of the IST month window containing `today`."""
    y, m = today.year, today.month
    if today.day < start_day:  # before the start day it is still the previous month's window
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(y, m, start_day), date(ny, nm, start_day)


def effective_limits(conn: sqlite3.Connection, vendor: str, plan: PlanSettings) -> Limits:
    """The plan's caps, each replaced by the latest override (`set_limit`) that sets that field."""
    daily, monthly = conn.execute(
        "SELECT (SELECT daily FROM vendor_limit WHERE vendor = :v AND daily IS NOT NULL "
        "ORDER BY id DESC LIMIT 1), (SELECT monthly FROM vendor_limit WHERE vendor = :v "
        "AND monthly IS NOT NULL ORDER BY id DESC LIMIT 1)",
        {"v": vendor},
    ).fetchone()
    return Limits(
        daily if daily is not None else plan.daily,
        monthly if monthly is not None else plan.monthly,
        "config" if daily is None else "override",
        "config" if monthly is None else "override",
    )


def _use(conn: sqlite3.Connection, vendor: str, first: date, end: date) -> tuple[int, int]:
    """(calls, of which manual) of the rows dated from `first` up to, not including, `end`."""
    used, manual = conn.execute(
        "SELECT COALESCE(SUM(calls), 0), "
        "COALESCE(SUM(CASE WHEN kind = 'manual' THEN calls END), 0) "
        "FROM vendor_call WHERE vendor = ? AND day_ist >= ? AND day_ist < ?",
        (vendor, first.isoformat(), end.isoformat()),
    ).fetchone()
    return int(used), int(manual)


def _status(
    conn: sqlite3.Connection, vendor: str, plan: PlanSettings, when: datetime
) -> QuotaStatus:
    today = ist_date(when)
    lim = effective_limits(conn, vendor, plan)
    tomorrow = today + timedelta(days=1)
    first, nxt = _month_window(today, plan.month_start_day)
    day = WindowUse("day", *_use(conn, vendor, today, tomorrow), lim.daily, lim.daily_source,
                    today, tomorrow)  # fmt: skip
    month = WindowUse("month", *_use(conn, vendor, first, nxt), lim.monthly, lim.monthly_source,
                      first, nxt)  # fmt: skip
    warnings = tuple(
        f"{vendor} {_ADJ[w.name]} quota at {w.used:,} of {w.limit:,} ({w.used * 100 // w.limit}%)"
        for w in (day, month)
        if w.limit is not None and w.used * 100 >= w.limit * WARN_PCT
    )
    return QuotaStatus(vendor, day, month, warnings)


def status_of(
    conn: sqlite3.Connection,
    vendor: str,
    plan: PlanSettings,
    *,
    now: Callable[[], datetime] = utcnow,
) -> QuotaStatus:
    return _status(conn, vendor, plan, now())


def used_on(conn: sqlite3.Connection, vendor: str, day: date) -> int:
    """Every call the ledger holds for `vendor` on one IST day, manual rows included."""
    return _use(conn, vendor, day, day + timedelta(days=1))[0]


def _refusal(vendor: str, status: QuotaStatus, reserve_pct: int) -> str | None:
    """Why a call that may use all but `reserve_pct` percent of each window must wait, if it must.
    The month is checked first so that, when both refuse, the later reset is the one reported."""
    for w in (status.month, status.day):
        if w.limit is None:
            continue
        floor = w.limit - w.limit * reserve_pct // 100
        if w.used >= floor:
            kept = (
                ""
                if w.used >= w.limit
                else f" ({w.limit - floor:,} kept for higher-priority calls)"
            )
            return (
                f"{vendor} {_ADJ[w.name]} quota reached: {w.used:,} of {w.limit:,} used{kept}; "
                f"resets {w.resets}"
            )
    return None


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction that takes the write lock first, so two processes cannot both read
    the same count. If BEGIN itself fails nothing is open: no rollback, the error propagates."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        with suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _insert_manual(
    conn: sqlite3.Connection, vendor: str, calls: int, when: datetime, day: date
) -> int:
    cur = conn.execute(
        "INSERT INTO vendor_call (vendor, called_at, day_ist, kind, calls) "
        "VALUES (?, ?, ?, 'manual', ?)",
        (vendor, to_iso(when), day.isoformat(), calls),
    )
    return int(cur.lastrowid or 0)


def _check_ceiling(what: str, n: int) -> None:
    if n > MAX_CALLS:
        raise NiveshError(f"{what} must be at most {MAX_CALLS:,}")


def add_manual(
    conn: sqlite3.Connection, vendor: str, calls: int, *, day: date | None = None,
    now: Callable[[], datetime] = utcnow,
) -> int:  # fmt: skip
    """Record `calls` spent outside this process (by another client of the same plan), dated on
    `day` (an IST date; default today). Returns the row id."""
    if calls < 1:
        raise NiveshError("calls must be 1 or more")
    _check_ceiling("calls", calls)
    when = now()
    return _insert_manual(conn, vendor, calls, when, day or ist_date(when))


def set_limit(
    conn: sqlite3.Connection, vendor: str, *, daily: int | None = None,
    monthly: int | None = None, now: Callable[[], datetime] = utcnow,
) -> None:  # fmt: skip
    """Append an override of the daily and/or monthly cap; the latest one per field wins."""
    if daily is None and monthly is None:
        raise NiveshError("give a daily or a monthly limit")
    if any(v is not None and v < 1 for v in (daily, monthly)):
        raise NiveshError("a limit must be 1 or more")
    for name, v in (("daily limit", daily), ("monthly limit", monthly)):
        if v is not None:
            _check_ceiling(name, v)
    conn.execute(
        "INSERT INTO vendor_limit (vendor, daily, monthly, set_at) VALUES (?, ?, ?, ?)",
        (vendor, daily, monthly, to_iso(now())),
    )


class Budget:
    """The quota gate of one vendor for one priority class of caller. A class may use all but
    `reserve_pct` percent of each window (holdings keep none back); the hard cap binds them all."""

    def __init__(
        self, conn: sqlite3.Connection, vendor: str, plan: PlanSettings, *,
        priority: Priority = "universe", now: Callable[[], datetime] = utcnow,
    ) -> None:  # fmt: skip
        self._conn, self._now = conn, now
        self.vendor, self.plan, self.priority = vendor, plan, priority
        self.attempts = 0  # reserves made by this instance

    def _reserve_pct(self) -> int:
        pct = self.plan.reserve_pct
        return {"holdings": 0, "watchlist": pct.watchlist, "universe": pct.universe}[self.priority]

    def reserve(self, tool: str, symbol: str | None = None) -> int:
        """Count one call BEFORE it is made and return its ledger id, or raise `QuotaExceeded`
        (naming the window, used and limit, and the reset) without writing anything."""
        when = self._now()
        with _immediate(self._conn):
            status = _status(self._conn, self.vendor, self.plan, when)
            if why := _refusal(self.vendor, status, self._reserve_pct()):
                raise QuotaExceeded(why)
            day = ist_date(when).isoformat()
            cur = self._conn.execute(
                "INSERT INTO vendor_call (vendor, called_at, day_ist, tool, symbol, priority) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.vendor, to_iso(when), day, tool, symbol, self.priority),
            )
        self.attempts += 1
        return int(cur.lastrowid or 0)

    def settle(self, call_id: int, outcome: Outcome) -> None:
        """Record how a reserved call ended; the count never changes and this works once."""
        self._conn.execute("UPDATE vendor_call SET outcome = ? WHERE id = ?", (outcome, call_id))

    def exhaust(self, window: WindowName) -> int:
        """Persist a vendor-reported halt: add one manual row that tops `window` up to its cap, so
        the next process refuses without spending a request. Never lowers anything and adds
        nothing when the window is at or past its cap or has none; returns the calls added. The
        month's row is dated on the window's first day, so it fills the month and not today (unless
        today is that first day)."""
        if window not in _ADJ:
            raise ValueError("window must be 'day' or 'month'")
        when = self._now()
        with _immediate(self._conn):
            st = _status(self._conn, self.vendor, self.plan, when)
            w = st.day if window == "day" else st.month
            gap = 0 if w.limit is None else w.limit - w.used
            if gap > 0:
                _insert_manual(self._conn, self.vendor, gap, when, w.starts_on)
        return max(gap, 0)

    def status(self) -> QuotaStatus:
        return _status(self._conn, self.vendor, self.plan, self._now())

    def describe(self) -> str:
        """The plan in words, from the effective limits: for messages and reports."""
        lim = effective_limits(self._conn, self.vendor, self.plan)
        caps, resets = [], []
        if lim.daily is not None:
            caps.append(f"daily {lim.daily:,}")
            resets.append("daily resets 00:00 IST")
        if lim.monthly is not None:
            caps.append(f"monthly {lim.monthly:,}")
            resets.append(f"monthly resets on day {self.plan.month_start_day} (IST)")
        return f"{', '.join(caps)}; {', '.join(resets)}"
