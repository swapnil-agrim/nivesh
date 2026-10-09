from datetime import UTC, date, datetime, timedelta, timezone


def utcnow() -> datetime:
    """Tz-aware UTC now; all stored timestamps are UTC (NFR-19)."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """UTC ISO-8601 text for storage; naive datetimes are rejected."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use a tz-aware UTC value")
    return dt.astimezone(UTC).isoformat()


IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST, so a fixed offset is exact


def ist_date(now: datetime) -> date:
    """Calendar date in IST; the broker session is valid only for one IST day (BR-12)."""
    if now.tzinfo is None:
        raise ValueError("naive datetime; use a tz-aware value")
    return now.astimezone(IST).date()
