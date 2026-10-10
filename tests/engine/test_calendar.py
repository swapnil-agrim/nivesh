from datetime import UTC, date, datetime, timedelta

import pytest

from nivesh_core.errors import CalendarUnknown
from nivesh_core.timeutil import IST
from nivesh_engine.calendar import Calendar, find_gaps, last_trading_day, nyse_holidays

NSE = Calendar.nse({2024: [date(2024, 1, 26), date(2024, 3, 25)], 2026: [date(2026, 1, 26)]})
NYSE = Calendar.nyse()


@pytest.mark.parametrize(
    "d",
    [
        date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
        date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
        date(2024, 11, 28), date(2024, 12, 25),
    ],
)  # fmt: skip
def test_nyse_2024_holidays_match_known_list(d: date) -> None:
    assert d in nyse_holidays(2024) and not NYSE.is_session(d)
    assert len(nyse_holidays(2024)) == 10


def test_nyse_saturday_holiday_observed_friday_and_sunday_observed_monday() -> None:
    assert date(2021, 7, 5) in nyse_holidays(2021)  # July 4 2021 was a Sunday
    assert date(2020, 7, 3) in nyse_holidays(2020)  # July 4 2020 was a Saturday
    assert date(2021, 12, 31) not in nyse_holidays(2021)  # Saturday New Year: Friday is open
    assert date(2023, 1, 2) in nyse_holidays(2023)  # Sunday New Year: Monday closed


@pytest.mark.parametrize("year, day", [(2023, date(2023, 4, 7)), (2025, date(2025, 4, 18)),
                                       (2026, date(2026, 4, 3))])  # fmt: skip
def test_nyse_good_friday_computed_for_several_years(year: int, day: date) -> None:
    assert day in nyse_holidays(year)


def test_nyse_early_close_days_are_still_sessions() -> None:
    assert NYSE.is_session(date(2024, 11, 29)) and NYSE.is_session(date(2024, 12, 24))


def test_weekends_are_not_sessions_for_both_markets() -> None:
    assert not NSE.is_session(date(2024, 3, 2)) and not NYSE.is_session(date(2024, 3, 3))


def test_nse_session_excludes_configured_holidays() -> None:
    assert not NSE.is_session(date(2024, 1, 26)) and NSE.is_session(date(2024, 1, 25))


def test_nse_year_without_config_raises_calendar_unknown() -> None:
    with pytest.raises(CalendarUnknown, match="2025"):
        NSE.is_session(date(2025, 1, 6))


def test_nse_dates_outside_covered_years_raise_not_guess() -> None:
    with pytest.raises(CalendarUnknown):
        NSE.sessions(date(2024, 12, 30), date(2025, 1, 2))


def test_find_gaps_ignores_holidays_and_weekends() -> None:
    have = [date(2024, 1, 24), date(2024, 1, 25), date(2024, 1, 29)]
    assert (
        find_gaps(NSE, have, date(2024, 1, 24), date(2024, 1, 29)) == []
    )  # 26 holiday, 27/28 wknd
    assert (
        find_gaps(NYSE, [date(2024, 3, 28), date(2024, 4, 1)], date(2024, 3, 28), date(2024, 4, 1))
        == []
    )


def test_find_gaps_reports_a_missing_session() -> None:
    assert find_gaps(NSE, [date(2024, 1, 24)], date(2024, 1, 24), date(2024, 1, 25)) == [
        date(2024, 1, 25)
    ]


def test_find_gaps_empty_range() -> None:
    assert find_gaps(NSE, [], date(2024, 1, 25), date(2024, 1, 24)) == []


def ist(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_last_trading_day_nse_before_and_after_1530_ist() -> None:
    assert last_trading_day(NSE, ist(2024, 3, 21, 15, 29)) == date(2024, 3, 20)
    assert last_trading_day(NSE, ist(2024, 3, 21, 15, 30)) == date(2024, 3, 21)


def test_last_trading_day_nse_on_sunday_returns_friday() -> None:
    assert last_trading_day(NSE, ist(2024, 3, 24, 11)) == date(2024, 3, 22)


def test_last_trading_day_nse_after_a_holiday_skips_it() -> None:
    assert last_trading_day(NSE, ist(2024, 3, 25, 18)) == date(2024, 3, 22)  # Holi Monday


def test_last_trading_day_nyse_uses_conservative_0230_ist_close() -> None:
    assert last_trading_day(NYSE, ist(2024, 3, 6, 2, 29)) == date(2024, 3, 4)
    assert last_trading_day(NYSE, ist(2024, 3, 6, 2, 30)) == date(2024, 3, 5)
    assert last_trading_day(NYSE, datetime(2024, 4, 1, 6, 0, tzinfo=UTC)) == date(2024, 3, 28)


def test_last_trading_day_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        last_trading_day(NSE, datetime(2024, 3, 21, 16))


def test_next_session_skips_weekend_and_holiday() -> None:
    assert NSE.next_session(date(2024, 1, 25)) == date(2024, 1, 29)
    assert NYSE.next_session(date(2024, 3, 28)) == date(2024, 4, 1)


def test_sessions_between_is_sorted_and_inclusive() -> None:
    s = NYSE.sessions(date(2024, 3, 25), date(2024, 4, 1))
    assert s == sorted(s) and s[0] == date(2024, 3, 25) and s[-1] == date(2024, 4, 1)
    assert date(2024, 3, 29) not in s and (s[-1] - s[0]) <= timedelta(days=7)
