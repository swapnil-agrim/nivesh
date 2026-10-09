from datetime import UTC, datetime


def utcnow() -> datetime:
    """Tz-aware UTC now; all stored timestamps are UTC (NFR-19)."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """UTC ISO-8601 text for storage; naive datetimes are rejected."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use a tz-aware UTC value")
    return dt.astimezone(UTC).isoformat()
