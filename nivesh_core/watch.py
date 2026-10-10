"""The watchlist (ST-9.5): securities the owner tracks, each with an optional entry zone. One row
per security; tracking it again replaces its zone. Decimals are stored as exact text. Nothing
here alerts or notifies: price alerts belong to a later epic.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nivesh_core.errors import NiveshError


@dataclass(frozen=True)
class Watch:
    security_id: int
    entry_low: Decimal | None
    entry_high: Decimal | None
    added_on: date


def _text(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


def track_security(
    conn: sqlite3.Connection, security_id: int, entry_low: Decimal | None,
    entry_high: Decimal | None, added_on: date,
) -> None:  # fmt: skip
    """Track a security, or replace the zone of one already tracked (`added_on` is kept)."""
    if (entry_low is None) != (entry_high is None):
        raise NiveshError("give both ends of the entry zone, or neither")
    if entry_low is not None and entry_high is not None:
        if not (entry_low.is_finite() and entry_high.is_finite()) or entry_low <= 0:
            raise NiveshError("the entry zone must be positive numbers")
        if entry_low > entry_high:
            raise NiveshError(f"entry zone: low {entry_low} is above high {entry_high}")
    conn.execute(
        "INSERT INTO watch (security_id, entry_low, entry_high, added_on) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(security_id) DO UPDATE SET entry_low = excluded.entry_low, "
        "entry_high = excluded.entry_high",
        (security_id, _text(entry_low), _text(entry_high), added_on.isoformat()),
    )


def watched(conn: sqlite3.Connection) -> list[Watch]:
    rows = conn.execute(
        "SELECT security_id, entry_low, entry_high, added_on FROM watch ORDER BY security_id"
    ).fetchall()
    return [
        Watch(
            int(r[0]),
            None if r[1] is None else Decimal(r[1]),
            None if r[2] is None else Decimal(r[2]),
            date.fromisoformat(r[3]),
        )  # fmt: skip
        for r in rows
    ]
