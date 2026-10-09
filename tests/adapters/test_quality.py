from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from nivesh_adapters.quality import (
    DataQualityError,
    check_non_negative,
    check_not_future,
    check_schema,
)


class Quote(BaseModel):
    symbol: str
    price: float


def test_error_carries_details() -> None:
    e = DataQualityError("yf", "price", -3, "must be >= 0")
    assert (e.adapter, e.field, e.value, e.reason) == ("yf", "price", -3, "must be >= 0")
    assert "yf" in str(e) and "price" in str(e) and "must be >= 0" in str(e)


def test_non_negative() -> None:
    check_non_negative("a", "price", 0)
    with pytest.raises(DataQualityError) as ei:
        check_non_negative("a", "price", -0.01)
    assert ei.value.value == -0.01


def test_not_future() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    check_not_future("a", "d", now, now=now)
    with pytest.raises(DataQualityError):
        check_not_future("a", "d", now + timedelta(days=1), now=now)
    with pytest.raises(DataQualityError, match="timezone"):
        check_not_future("a", "d", datetime(2026, 1, 1), now=now)  # noqa: DTZ001


def test_schema_validator() -> None:
    assert check_schema("a", Quote, {"symbol": "X", "price": 1}).price == 1
    with pytest.raises(DataQualityError) as ei:
        check_schema("a", Quote, {"symbol": "X", "price": "abc"})
    assert ei.value.field == "price"
