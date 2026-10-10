"""Trading calendars for NSE and NYSE (BR-11). Pure: holiday data comes in as arguments.

NYSE holidays are rule-computed (observed-day rules, Good Friday from the Gregorian Easter).
NSE holidays are gazette-announced, so the caller passes them; a year without data raises
`CalendarUnknown` instead of silently assuming weekdays are sessions.
Early-close days (day after Thanksgiving, Christmas Eve) are still sessions.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from nivesh_core.errors import CalendarUnknown
from nivesh_core.timeutil import IST, ist_date

Market = Literal["NSE", "NYSE"]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_monday(year: int, month: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=last.weekday())


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=d.weekday() == 6)


def nyse_holidays(year: int) -> frozenset[date]:
    fixed = [date(year, 7, 4), date(year, 12, 25)]
    if year >= 2022:
        fixed.append(date(year, 6, 19))
    out = {_observed(d) for d in fixed}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # Saturday Jan 1: the Friday before is a session
        out.add(_observed(new_year))
    out |= {
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_monday(year, 5),  # Memorial
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    return frozenset(out)


@dataclass(frozen=True)
class Calendar:
    market: Market
    holidays: dict[int, frozenset[date]] | None = None  # None = rule-computed (NYSE)

    @classmethod
    def nyse(cls) -> "Calendar":
        return cls("NYSE")

    @classmethod
    def nse(cls, holidays: dict[int, Collection[date]]) -> "Calendar":
        return cls("NSE", {y: frozenset(v) for y, v in holidays.items()})

    def _year_holidays(self, year: int) -> frozenset[date]:
        if self.holidays is None:
            return nyse_holidays(year)
        if year not in self.holidays:
            raise CalendarUnknown(
                f"no {self.market} holiday data for {year}; add it under market.nse_holidays"
            )
        return self.holidays[year]

    def is_session(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._year_holidays(d.year)

    def sessions(self, start: date, end: date) -> list[date]:
        days = (start + timedelta(days=i) for i in range((end - start).days + 1))
        return [d for d in days if self.is_session(d)]

    def next_session(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_session(d):
            d += timedelta(days=1)
        return d

    def close_at(self, d: date) -> datetime:
        """Session close as an aware instant. NYSE uses 02:30 IST of the next date (conservative
        EST close; an hour early in US summer time)."""
        if self.market == "NSE":
            return datetime.combine(d, time(15, 30), tzinfo=IST)
        return datetime.combine(d + timedelta(days=1), time(2, 30), tzinfo=IST)


def find_gaps(cal: Calendar, have: Iterable[date], start: date, end: date) -> list[date]:
    """Sessions in [start, end] with no data; weekends and holidays are never gaps."""
    got = set(have)
    return [d for d in cal.sessions(start, end) if d not in got]


def last_trading_day(cal: Calendar, now: datetime) -> date:
    """Latest session whose close has passed at the aware instant `now`, as an IST date."""
    today = ist_date(now)  # rejects naive datetimes
    for back in range(0, 15):
        d = today - timedelta(days=back)
        if cal.is_session(d) and cal.close_at(d) <= now:
            return d
    raise CalendarUnknown(f"no {cal.market} session found in the 15 days before {today}")
