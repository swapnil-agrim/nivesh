import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.errors import NiveshError
from nivesh_core.watch import Watch, track_security, watched

D = Decimal
DAY = date(2026, 1, 2)


def db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, MIGRATIONS / "sqlite")
    for i, s in enumerate(("A", "B"), start=1):
        c.execute(
            "INSERT INTO security (id, symbol, exchange, currency) VALUES (?, ?, 'NSE', 'INR')",
            (i, s),
        )
    return c


def test_track_security_with_and_without_zone() -> None:
    c = db()
    track_security(c, 1, D("90.50"), D("100"), DAY)
    track_security(c, 2, None, None, DAY)
    assert watched(c) == [Watch(1, D("90.50"), D("100"), DAY), Watch(2, None, None, DAY)]
    assert str(watched(c)[0].entry_low) == "90.50"  # exact text round trip


def test_track_again_updates_zone_via_replace_semantics_not_a_second_row() -> None:
    c = db()
    track_security(c, 1, D(90), D(100), DAY)
    track_security(c, 1, D(80), D(95), date(2026, 2, 1))
    assert watched(c) == [Watch(1, D(80), D(95), DAY)]  # zone replaced, first added_on kept
    track_security(c, 1, None, None, date(2026, 3, 1))
    assert watched(c) == [Watch(1, None, None, DAY)]


def test_zone_low_greater_than_high_refused() -> None:
    c = db()
    with pytest.raises(NiveshError, match="low 100 is above high 90"):
        track_security(c, 1, D(100), D(90), DAY)
    with pytest.raises(NiveshError, match="both ends"):
        track_security(c, 1, D(1), None, DAY)
    with pytest.raises(NiveshError, match="positive"):
        track_security(c, 1, D(0), D(5), DAY)
    with pytest.raises(NiveshError, match="positive"):
        track_security(c, 1, D("NaN"), D(5), DAY)
    assert watched(c) == []


def test_equal_ends_are_a_valid_zone() -> None:
    c = db()
    track_security(c, 1, D(50), D(50), DAY)
    assert watched(c)[0].entry_high == D(50)
