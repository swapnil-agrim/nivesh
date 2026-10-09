from datetime import UTC, date, datetime

import pytest

from nivesh_core.timeutil import ist_date


def test_ist_date_rolls_over_at_1830_utc() -> None:
    assert ist_date(datetime(2026, 1, 5, 18, 29, tzinfo=UTC)) == date(2026, 1, 5)
    assert ist_date(datetime(2026, 1, 5, 18, 31, tzinfo=UTC)) == date(2026, 1, 6)


def test_ist_date_requires_tz_aware() -> None:
    with pytest.raises(ValueError, match="naive"):
        ist_date(datetime(2026, 1, 5, 12, 0))
