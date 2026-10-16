import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from nivesh_core import quota
from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.errors import (
    MarketDataError,
    NiveshError,
    NotConfigured,
    QuotaExceeded,
    RateLimited,
    SourceUnavailable,
)
from nivesh_core.fundamentals_config import PlanSettings, ReservePct
from nivesh_core.pii_scan import scan_text
from nivesh_core.quota import (
    Budget,
    add_manual,
    effective_limits,
    set_limit,
    status_of,
    used_on,
)

SQLITE = MIGRATIONS / "sqlite"
V = "tapetide"
NOON = datetime(2026, 10, 9, 6, 30, tzinfo=UTC)  # 12:00 IST on 2026-10-09
DAY = date(2026, 10, 9)


def connect(path: Path | str, timeout: float = 30) -> sqlite3.Connection:
    c = sqlite3.connect(path, isolation_level=None, timeout=timeout)  # autocommit, as in prod
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def conn(path: Path | str = ":memory:", timeout: float = 30) -> sqlite3.Connection:
    c = connect(path, timeout)
    migrate.apply(c, SQLITE)
    return c


def plan(daily: int | None = 400, monthly: int | None = 5000, **kw: Any) -> PlanSettings:
    return PlanSettings(daily=daily, monthly=monthly, **kw)


def at(when: datetime) -> Callable[[], datetime]:
    return lambda: when


def budget(
    c: sqlite3.Connection, p: PlanSettings | None = None, priority: Any = "universe",
    when: datetime = NOON,
) -> Budget:  # fmt: skip
    return Budget(c, V, p or plan(), priority=priority, now=at(when))


def rows(c: sqlite3.Connection, where: str = "1") -> list[tuple[Any, ...]]:
    return c.execute(
        "SELECT kind, calls, day_ist, tool, symbol, priority, outcome FROM vendor_call "  # noqa: S608
        f"WHERE {where} ORDER BY id"
    ).fetchall()


def test_the_error_classes_sit_where_the_callers_expect() -> None:
    for cls in (NotConfigured, QuotaExceeded):
        assert issubclass(cls, MarketDataError) and issubclass(cls, NiveshError)
    assert issubclass(NotConfigured, SourceUnavailable) and issubclass(QuotaExceeded, RateLimited)
    assert not issubclass(NotConfigured, RateLimited)
    assert not issubclass(QuotaExceeded, SourceUnavailable)


# --- reserve: caps, floors, windows ---


def test_the_first_call_is_allowed_and_recorded() -> None:
    c = conn()
    b = budget(c, priority="watchlist")
    assert b.reserve("get_financials", "RELIANCE") == 1
    assert b.attempts == 1 and not c.in_transaction
    assert rows(c) == [("call", 1, "2026-10-09", "get_financials", "RELIANCE", "watchlist", None)]
    got = c.execute("SELECT vendor, called_at FROM vendor_call").fetchone()
    assert got == (V, "2026-10-09T06:30:00+00:00")  # UTC ISO text
    assert b.reserve("get_shareholding") == 2
    assert rows(c, "id = 2")[0][3:5] == ("get_shareholding", None) and b.attempts == 2


@pytest.mark.parametrize("window", ["daily", "monthly"])
def test_the_cap_th_call_is_allowed_and_the_next_is_refused(window: str) -> None:
    c = conn()
    p = plan(daily=5, monthly=None) if window == "daily" else plan(daily=None, monthly=5)
    b = budget(c, p, priority="holdings")
    for _ in range(5):
        b.reserve("t")
    with pytest.raises(QuotaExceeded) as e:
        b.reserve("t")
    msg = str(e.value)
    assert isinstance(e.value, RateLimited)
    assert window in msg and "5 of 5" in msg and "00:00 IST" in msg and V in msg
    assert ("2026-11-01" in msg) == (window == "monthly")  # the month names its reset date
    assert len(rows(c)) == 5 and b.attempts == 5  # a refusal writes nothing


def test_when_both_windows_refuse_the_one_that_resets_later_is_reported() -> None:
    both = budget(conn(), plan(daily=2, monthly=2), priority="holdings")
    both.reserve("t")
    both.reserve("t")
    with pytest.raises(QuotaExceeded, match="monthly"):
        both.reserve("t")
    day_only = budget(conn(), plan(daily=1, monthly=100), priority="holdings")
    day_only.reserve("t")
    with pytest.raises(QuotaExceeded, match="daily"):
        day_only.reserve("t")


@pytest.mark.parametrize(
    ("priority", "daily_floor", "monthly_floor"),
    [("universe", 280, 3500), ("watchlist", 360, 4500), ("holdings", 400, 5000)],
)
def test_priority_floors_hold_back_the_top_of_each_window(
    priority: str, daily_floor: int, monthly_floor: int
) -> None:
    c = conn()
    add_manual(c, V, daily_floor - 1, now=at(NOON))
    b = budget(c, priority=priority)
    b.reserve("t")  # the last call this class may use: the daily_floor-th
    with pytest.raises(QuotaExceeded, match=rf"daily .*{daily_floor} of 400") as e:
        b.reserve("t")
    assert ("higher-priority" in str(e.value)) == (priority != "holdings")
    m = conn()
    only_month = plan(daily=None, monthly=5000)
    add_manual(m, V, monthly_floor - 1, day=date(2026, 10, 1), now=at(NOON))
    bm = budget(m, only_month, priority)
    bm.reserve("t")
    with pytest.raises(QuotaExceeded, match=rf"monthly .*{monthly_floor:,} of 5,000"):
        bm.reserve("t")


def test_the_hard_cap_applies_to_every_class_and_a_higher_class_outlasts_a_lower_one() -> None:
    c = conn()
    add_manual(c, V, 300, now=at(NOON))
    with pytest.raises(QuotaExceeded):
        budget(c, priority="universe").reserve("t")  # 300 > the universe floor of 280
    budget(c, priority="watchlist").reserve("t")  # 300 < 360
    budget(c, priority="holdings").reserve("t")
    add_manual(c, V, 98, now=at(NOON))  # 400 used
    for priority in ("universe", "watchlist", "holdings"):
        with pytest.raises(QuotaExceeded):
            budget(c, priority=priority).reserve("t")


def test_the_ist_day_changes_at_1830_utc_not_at_midnight_utc() -> None:
    c = conn()
    p = plan(daily=3, monthly=None)
    last = datetime(2026, 10, 9, 18, 29, 59, tzinfo=UTC)  # 23:59:59 IST
    first = datetime(2026, 10, 9, 18, 30, 0, tzinfo=UTC)  # 00:00:00 IST the next day
    b = budget(c, p, "holdings", last)
    for _ in range(3):
        b.reserve("t")
    with pytest.raises(QuotaExceeded):
        b.reserve("t")
    assert {r[2] for r in rows(c)} == {"2026-10-09"}
    nxt = budget(c, p, "holdings", first)
    nxt.reserve("t")  # a full day D ledger accepts again on D+1
    assert rows(c)[-1][2] == "2026-10-10"
    after_utc_midnight = datetime(2026, 10, 10, 0, 5, tzinfo=UTC)  # 05:35 IST: still D+1
    assert budget(c, p, "holdings", after_utc_midnight).status().day.used == 1


@pytest.mark.parametrize(
    ("start", "today", "window_first", "window_last", "resets"),
    [
        (1, date(2026, 10, 9), date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1)),
        (1, date(2026, 12, 31), date(2026, 12, 1), date(2026, 12, 31), date(2027, 1, 1)),
        (5, date(2026, 10, 9), date(2026, 10, 5), date(2026, 11, 4), date(2026, 11, 5)),
        (5, date(2026, 10, 5), date(2026, 10, 5), date(2026, 11, 4), date(2026, 11, 5)),
        (5, date(2026, 10, 4), date(2026, 9, 5), date(2026, 10, 4), date(2026, 10, 5)),
        (5, date(2026, 1, 3), date(2025, 12, 5), date(2026, 1, 4), date(2026, 1, 5)),
        (5, date(2026, 12, 20), date(2026, 12, 5), date(2027, 1, 4), date(2027, 1, 5)),
        (28, date(2026, 2, 27), date(2026, 1, 28), date(2026, 2, 27), date(2026, 2, 28)),
    ],
)
def test_the_month_window_starts_on_month_start_day_and_rolls_over_years(
    start: int, today: date, window_first: date, window_last: date, resets: date
) -> None:
    c = conn()
    p = plan(daily=None, monthly=100, month_start_day=start)
    now = datetime(today.year, today.month, today.day, 6, 30, tzinfo=UTC)
    for day in (window_first, window_last):
        add_manual(c, V, 1, day=day, now=at(now))
    for day in (date.fromordinal(window_first.toordinal() - 1), resets):  # just outside
        add_manual(c, V, 10, day=day, now=at(now))
    w = status_of(c, V, p, now=at(now)).month
    assert w.used == 2 and w.resets_on == resets
    assert w.resets == f"00:00 IST on {resets.isoformat()}"


def test_a_monthly_window_counts_rows_of_every_day_in_it() -> None:
    c = conn()
    p = plan(daily=400, monthly=5)
    for day in (1, 2, 3, 4):
        add_manual(c, V, 1, day=date(2026, 10, day), now=at(NOON))
    b = budget(c, p, "holdings")
    b.reserve("t")  # the fifth call of the month, on another day
    with pytest.raises(QuotaExceeded, match="monthly"):
        b.reserve("t")


# --- overrides and manual entries ---


def test_effective_limits_are_the_config_until_an_override_replaces_a_field() -> None:
    c = conn()
    p = plan()
    lim = effective_limits(c, V, p)
    assert (lim.daily, lim.monthly, lim.daily_source, lim.monthly_source) == (
        400, 5000, "config", "config",
    )  # fmt: skip
    set_limit(c, V, daily=300, now=at(NOON))
    lim = effective_limits(c, V, p)
    assert (lim.daily, lim.monthly, lim.daily_source, lim.monthly_source) == (
        300, 5000, "override", "config",
    )  # fmt: skip
    set_limit(c, V, daily=200, now=at(NOON))
    set_limit(c, V, monthly=1000, now=at(NOON))  # a later row for the other field keeps daily
    set_limit(c, "other", daily=9, now=at(NOON))  # another vendor is separate
    lim = effective_limits(c, V, p)
    assert (lim.daily, lim.monthly) == (200, 1000)
    set_limit(c, V, daily=1000, now=at(NOON))  # an override may also raise the configured limit
    assert effective_limits(c, V, p).daily == 1000
    assert effective_limits(c, V, plan(daily=None, monthly=7)).daily == 1000  # even where unset
    assert c.execute("SELECT set_at FROM vendor_limit").fetchone() == ("2026-10-09T06:30:00+00:00",)


def test_set_limit_needs_a_positive_number() -> None:
    c = conn()
    for kw in ({}, {"daily": 0}, {"monthly": -1}, {"daily": 5, "monthly": 0}):
        with pytest.raises(NiveshError):
            set_limit(c, V, **kw)
    assert c.execute("SELECT count(*) FROM vendor_limit").fetchone() == (0,)


def test_the_budget_enforces_the_override_not_the_config() -> None:
    c = conn()
    set_limit(c, V, daily=2)
    b = budget(c, priority="holdings")
    b.reserve("t")
    b.reserve("t")
    with pytest.raises(QuotaExceeded, match="2 of 2"):
        b.reserve("t")
    assert budget(c).status().day.source == "override"


def test_manual_calls_count_toward_both_windows_and_status_shows_them() -> None:
    c = conn()
    assert add_manual(c, V, 120, now=at(NOON)) == 1
    assert rows(c) == [("manual", 120, "2026-10-09", None, None, None, None)]
    b = budget(c, priority="holdings")
    b.reserve("t")
    s = b.status()
    assert (s.day.used, s.day.manual, s.month.used, s.month.manual) == (121, 120, 121, 120)
    assert (s.day.remaining, s.month.remaining) == (279, 4879)
    add_manual(c, V, 7, day=date(2026, 10, 2), now=at(NOON))  # earlier in the month: month only
    s = b.status()
    assert (s.day.used, s.day.manual, s.month.used, s.month.manual) == (121, 120, 128, 127)
    assert rows(c, "day_ist = '2026-10-02'") == [
        ("manual", 7, "2026-10-02", None, None, None, None)
    ]


def test_the_ledger_queries_raise_no_sqlite_deprecation_warning() -> None:
    """Python 3.14 turns sqlite3's mixing of named and positional placeholders into an error."""
    import warnings

    assert not re.search(r"\?\d", Path(quota.__file__).read_text())  # numbered placeholders
    c = conn()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        set_limit(c, V, daily=50, monthly=500, now=at(NOON))
        add_manual(c, V, 3, now=at(NOON))
        b = budget(c, priority="holdings")
        b.reserve("t")
        b.status()
        b.exhaust("month")
        effective_limits(c, V, plan())


def test_add_manual_rejects_a_non_positive_count() -> None:
    c = conn()
    for n in (0, -3):
        with pytest.raises(NiveshError):
            add_manual(c, V, n)
    assert rows(c) == []


def test_counts_above_the_ceiling_are_rejected_and_nothing_is_written() -> None:
    """An append-only ledger cannot take a typo back, and a huge count would overflow SQLite's
    64-bit SUM (every reserve would then fail closed for good)."""
    c = conn()
    assert quota.MAX_CALLS == 1_000_000
    for n in (quota.MAX_CALLS + 1, 2**63 - 1, 10**30):
        with pytest.raises(NiveshError, match="at most 1,000,000"):
            add_manual(c, V, n)
        with pytest.raises(NiveshError, match="at most 1,000,000"):
            set_limit(c, V, daily=n)
        with pytest.raises(NiveshError, match="at most 1,000,000"):
            set_limit(c, V, monthly=n)
    assert rows(c) == []
    assert c.execute("SELECT count(*) FROM vendor_limit").fetchone() == (0,)
    add_manual(c, V, quota.MAX_CALLS, now=at(NOON))  # the ceiling itself is allowed
    set_limit(c, V, daily=quota.MAX_CALLS, monthly=quota.MAX_CALLS)
    assert budget(c, priority="holdings").status().day.used == quota.MAX_CALLS


# --- never refunded, crashes, races ---


def test_nothing_is_ever_refunded() -> None:
    c = conn()
    b = budget(c, priority="holdings")
    call = b.reserve("t")
    b.settle(call, "error")
    assert b.status().day.used == 1 and rows(c)[0][-1] == "error"
    with pytest.raises(sqlite3.DatabaseError, match="set once"):
        b.settle(call, "ok")  # an outcome is set once, so a failure is never re-labelled billed
    for sql in ("DELETE FROM vendor_call", "UPDATE vendor_call SET calls = 1"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(sql)
    src = Path(quota.__file__).read_text()
    assert "DELETE" not in src and re.findall(r"UPDATE \w+ SET (\w+)", src) == ["outcome"]


def test_settle_records_each_outcome_once_and_rejects_an_unknown_one() -> None:
    c = conn()
    b = budget(c)
    ids = [b.reserve("t") for _ in range(3)]
    for call, outcome in zip(ids, ("ok", "error", "halted"), strict=True):
        b.settle(call, outcome)
    assert [r[-1] for r in rows(c)] == ["ok", "error", "halted"]
    with pytest.raises(sqlite3.IntegrityError):
        b.settle(b.reserve("t"), "fine")


class Spy:
    """The few connection methods the ledger uses, recording the SQL it sends."""

    def __init__(self, inner: sqlite3.Connection, boom: str | None = None) -> None:
        self.inner, self.boom = inner, boom
        self.sent: list[str] = []

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        self.sent.append(" ".join(sql.split()[:2]))
        if self.boom and sql.lstrip().upper().startswith(self.boom):
            raise RuntimeError("disk exploded")
        return self.inner.execute(sql, *args)


def test_a_crashed_reserve_rolls_back_and_leaves_no_row() -> None:
    c = conn()
    spy = Spy(c, boom="INSERT")
    b = Budget(spy, V, plan(), now=at(NOON))
    with pytest.raises(RuntimeError, match="exploded"):
        b.reserve("t")
    assert rows(c) == [] and not c.in_transaction and b.attempts == 0
    assert spy.sent[0] == "BEGIN IMMEDIATE" and spy.sent[-1] == "ROLLBACK"
    spy.boom = None
    assert b.reserve("t") == 1  # the connection and the lock are usable again


def test_a_refused_reserve_releases_the_write_lock() -> None:
    c = conn()
    b = budget(c, plan(daily=1, monthly=None), "holdings")
    b.reserve("t")
    with pytest.raises(QuotaExceeded):
        b.reserve("t")
    assert not c.in_transaction


def test_a_crash_after_the_commit_over_counts_by_one() -> None:
    c = conn()
    b = budget(c)
    b.reserve("t")  # the process dies here: no settle, the request may or may not have left
    assert rows(c)[0][-1] is None and b.status().day.used == 1


def test_a_failing_begin_is_not_rolled_back_and_propagates_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    main = conn(path, timeout=0.05)
    spy = Spy(main)
    b = Budget(spy, V, plan(), now=at(NOON))
    holder = conn(path)
    holder.execute("BEGIN IMMEDIATE")  # another process holds the write lock
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        b.reserve("t")
    assert spy.sent == ["BEGIN IMMEDIATE"]  # no ROLLBACK would only mask the real error
    assert b.attempts == 0
    holder.execute("ROLLBACK")
    assert b.reserve("t") == 1
    assert len(rows(main)) == 1


def test_threads_on_separate_connections_never_exceed_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    conn(path).close()
    p = plan(daily=10, monthly=None)

    def worker() -> tuple[int, int]:
        c = connect(path)
        b = Budget(c, V, p, priority="holdings", now=at(NOON))
        ok = refused = 0
        for _ in range(10):
            try:
                b.reserve("t")
                ok += 1
            except QuotaExceeded:
                refused += 1
        c.close()
        return ok, refused

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: worker(), range(4)))
    assert sum(r[0] for r in results) == 10 and sum(r[1] for r in results) == 30
    assert len(rows(conn(path))) == 10


# --- status, warnings, wording ---


def test_status_of_a_fresh_ledger() -> None:
    s = budget(conn()).status()
    assert s.vendor == V and s.warnings == ()
    assert (s.day.name, s.day.used, s.day.limit, s.day.remaining) == ("day", 0, 400, 400)
    assert (s.day.source, s.day.manual, s.day.resets) == ("config", 0, "00:00 IST")
    assert s.day.resets_on == date(2026, 10, 10)
    assert (s.month.name, s.month.used, s.month.limit, s.month.remaining) == (
        "month",
        0,
        5000,
        5000,
    )
    assert s.month.resets_on == date(2026, 11, 1)


def test_a_window_without_a_cap_counts_but_never_refuses_or_warns() -> None:
    c = conn()
    b = budget(c, plan(daily=2, monthly=None), "holdings")
    add_manual(c, V, 1, day=date(2026, 10, 1), now=at(NOON))
    s = b.status()
    assert (s.month.limit, s.month.remaining, s.month.used) == (None, None, 1)
    assert s.warnings == ()
    b.reserve("t")
    b.reserve("t")
    with pytest.raises(QuotaExceeded, match="daily"):
        b.reserve("t")


def test_the_warning_appears_at_80_percent_of_either_window() -> None:
    assert quota.WARN_PCT == 80
    c = conn()
    add_manual(c, V, 319, now=at(NOON))
    assert budget(c).status().warnings == ()
    add_manual(c, V, 1, now=at(NOON))  # 320 of 400
    warnings = budget(c).status().warnings
    assert len(warnings) == 1 and "daily" in warnings[0] and "320 of 400" in warnings[0]
    m = conn()
    add_manual(m, V, 4000, day=date(2026, 10, 1), now=at(NOON))  # 4,000 of 5,000, day untouched
    warnings = budget(m).status().warnings
    assert len(warnings) == 1 and "monthly" in warnings[0] and "4,000 of 5,000" in warnings[0]
    add_manual(m, V, 320, now=at(NOON))
    assert len(budget(m).status().warnings) == 2


def test_describe_states_the_configured_plan_and_nothing_else() -> None:
    c = conn()
    d = budget(c).describe()
    assert d == "daily 400, monthly 5,000; daily resets 00:00 IST, monthly resets on day 1 (IST)"
    assert "free plan" not in d and "50" not in d
    only_month = budget(c, plan(daily=None, monthly=2000, month_start_day=5)).describe()
    assert only_month == "monthly 2,000; monthly resets on day 5 (IST)"
    set_limit(c, V, daily=300)
    assert budget(c).describe().startswith("daily 300, monthly 5,000;")  # the override shows


def test_every_ledger_row_passes_the_pii_scan_over_the_whole_dump() -> None:
    c = conn()
    b = budget(c, priority="holdings")
    b.settle(b.reserve("get_financials", "RELIANCE"), "ok")
    b.settle(b.reserve("get_shareholding", "HDFCBANK"), "error")
    add_manual(c, V, 120, day=date(2026, 10, 2), now=at(NOON))
    set_limit(c, V, daily=300, monthly=4000, now=at(NOON))
    b.exhaust("day")
    dump = "\n".join(c.iterdump())
    assert "vendor_call" in dump and scan_text(dump) == []


# --- exhaust: persisting a vendor-reported halt (refinement 4a) ---


def test_exhaust_day_tops_today_up_to_the_daily_cap_with_one_manual_row() -> None:
    c = conn()
    b = budget(c, priority="holdings")
    for _ in range(5):
        b.reserve("t")
    assert b.exhaust("day") == 395
    assert rows(c)[-1] == ("manual", 395, "2026-10-09", None, None, None, None)
    s = b.status()
    assert s.day.used == 400 and s.month.used == 400 and b.attempts == 5  # not an attempt
    with pytest.raises(QuotaExceeded, match="400 of 400"):
        b.reserve("t")
    assert b.exhaust("day") == 0 and len(rows(c)) == 6  # nothing more to add


def test_exhaust_month_fills_the_month_on_its_first_day_not_today() -> None:
    c = conn()
    b = budget(c, priority="holdings")
    for _ in range(7):
        b.reserve("t")
    assert b.exhaust("month") == 4993
    assert rows(c)[-1] == ("manual", 4993, "2026-10-01", None, None, None, None)
    s = b.status()
    assert (s.month.used, s.day.used) == (5000, 7)  # the day keeps its room
    with pytest.raises(QuotaExceeded, match="monthly"):
        b.reserve("t")


def test_exhaust_month_dates_the_row_on_the_window_start_of_a_custom_month() -> None:
    c = conn()
    early = datetime(2026, 10, 3, 6, 30, tzinfo=UTC)  # before day 5: the window began 2026-09-05
    b = budget(c, plan(daily=None, monthly=50, month_start_day=5), "holdings", early)
    b.reserve("t")
    assert b.exhaust("month") == 49
    assert rows(c)[-1][2] == "2026-09-05" and b.status().month.used == 50


def test_exhaust_month_on_the_first_day_of_the_window_also_fills_that_day() -> None:
    c = conn()
    first = datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
    b = budget(c, plan(daily=40, monthly=50), "holdings", first)
    assert b.exhaust("month") == 50  # the row is dated today, so today is full as well
    assert b.status().day.used == 50 and b.status().month.used == 50


def test_exhaust_never_lowers_anything() -> None:
    c = conn()
    set_limit(c, V, daily=3)  # the override lowered the cap below what is already used
    add_manual(c, V, 10, now=at(NOON))
    b = budget(c, priority="holdings")
    assert b.exhaust("day") == 0 and b.exhaust("month") == 4990
    assert [r[1] for r in rows(c)] == [10, 4990]
    s = b.status()
    assert (s.day.used, s.month.used) == (10, 5000)
    assert b.exhaust("month") == 0 and len(rows(c)) == 2


def test_exhaust_uses_the_effective_limit_and_ignores_an_uncapped_window() -> None:
    c = conn()
    set_limit(c, V, daily=300)
    b = budget(c, priority="holdings")
    assert b.exhaust("day") == 300 and b.status().day.used == 300
    only_day = budget(conn(), plan(daily=9, monthly=None), "holdings")
    assert only_day.exhaust("month") == 0 and only_day.status().month.used == 0
    m = conn()
    assert budget(m, plan(daily=None, monthly=9), "holdings").exhaust("day") == 0 and rows(m) == []


def test_exhaust_rejects_an_unknown_window() -> None:
    with pytest.raises(ValueError, match="window"):
        budget(conn()).exhaust("week")


def test_two_racing_exhausts_add_the_gap_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    conn(path).close()

    def worker(_: int) -> int:
        c = connect(path)
        try:
            return Budget(c, V, plan(), priority="holdings", now=at(NOON)).exhaust("day")
        finally:
            c.close()

    with ThreadPoolExecutor(4) as pool:
        added = list(pool.map(worker, range(4)))
    assert sorted(added) == [0, 0, 0, 400]
    assert status_of(conn(path), V, plan(), now=at(NOON)).day.used == 400


def test_reserve_priority_floors_after_an_exhaust_hold_for_every_class() -> None:
    c = conn()
    budget(c).exhaust("day")
    for priority in ("universe", "watchlist", "holdings"):
        with pytest.raises(QuotaExceeded):
            budget(c, priority=priority).reserve("t")


def test_reserve_pct_zero_lets_a_lower_class_use_the_whole_window() -> None:
    c = conn()
    p = plan(daily=10, monthly=None, reserve_pct=ReservePct(watchlist=0, universe=0))
    b = budget(c, p, "universe")
    for _ in range(10):
        b.reserve("t")
    with pytest.raises(QuotaExceeded):
        b.reserve("t")


def test_used_on_counts_one_ist_day_of_both_kinds_and_only_that_vendor() -> None:
    c = conn()
    b = budget(c)
    b.reserve("get_financials", "X")
    b.reserve("get_financials", "X")
    add_manual(c, V, 5, now=at(NOON))
    add_manual(c, V, 7, day=date(2026, 10, 8), now=at(NOON))
    add_manual(c, "other", 9, now=at(NOON))
    assert used_on(c, V, DAY) == 7 and used_on(c, V, date(2026, 10, 8)) == 7
    assert used_on(c, V, date(2026, 10, 10)) == 0 and used_on(c, "other", DAY) == 9
