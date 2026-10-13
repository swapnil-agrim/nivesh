"""Built-in NSE trading holidays (weekday closures only) so a fresh install can fetch prices.

Verified 2026-10-13: 2026 against the NSE holiday-master feed and bhavcopy 404s; 2024 and 2025
from bhavcopy 404 scans. Weekend-only holidays are omitted (weekends are never sessions) and
special weekend sessions (Muhurat, Saturday 2024-01-20 and 2024-05-18) are not representable.
Extend yearly from the NSE circular: a year with no data anywhere still raises `CalendarUnknown`.
"""

from collections.abc import Iterable, Mapping
from datetime import date


def _days(year: int, *month_day: tuple[int, int]) -> frozenset[date]:
    return frozenset(date(year, m, d) for m, d in month_day)


DEFAULT_NSE_HOLIDAYS: dict[int, frozenset[date]] = {
    2024: _days(
        2024, (1, 22), (1, 26), (3, 8), (3, 25), (3, 29), (4, 11), (4, 17), (5, 1), (5, 20),
        (6, 17), (7, 17), (8, 15), (10, 2), (11, 15), (11, 20), (12, 25),
    ),
    2025: _days(
        2025, (2, 26), (3, 14), (3, 31), (4, 10), (4, 14), (4, 18), (5, 1), (8, 15), (8, 27),
        (10, 2), (10, 22), (11, 5), (12, 25),
    ),
    2026: _days(
        2026, (1, 15), (1, 26), (3, 3), (3, 26), (3, 31), (4, 3), (4, 14), (5, 1), (5, 28),
        (6, 26), (9, 14), (10, 2), (10, 20), (11, 10), (11, 24), (12, 25),
    ),
}  # fmt: skip
BUILTIN_YEARS: tuple[int, ...] = tuple(sorted(DEFAULT_NSE_HOLIDAYS))


def merged_nse_holidays(configured: Mapping[int, Iterable[date]]) -> dict[int, frozenset[date]]:
    """Per-year union of the built-in lists and the configured ones. Config is additive: the
    built-in dates are the verified set, so retracting one through config is not supported."""
    out = dict(DEFAULT_NSE_HOLIDAYS)
    for year, days in configured.items():
        out[year] = out.get(year, frozenset()) | frozenset(days)
    return out
