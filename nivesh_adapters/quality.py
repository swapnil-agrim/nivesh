from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from nivesh_core.errors import NiveshError
from nivesh_core.timeutil import utcnow

M = TypeVar("M", bound=BaseModel)


class DataQualityError(NiveshError):
    def __init__(self, adapter: str, field: str, value: Any, reason: str) -> None:
        self.adapter, self.field, self.value, self.reason = adapter, field, value, reason
        super().__init__(f"{adapter}: bad {field} ({reason}); value={value!r}")


def check_non_negative(adapter: str, field: str, value: float | Decimal) -> None:
    if value < 0:
        raise DataQualityError(adapter, field, value, "must be >= 0")


def check_not_future(
    adapter: str, field: str, value: datetime, *, now: datetime | None = None
) -> None:
    if value.tzinfo is None:
        raise DataQualityError(adapter, field, value, "missing timezone")
    if value > (now or utcnow()):
        raise DataQualityError(adapter, field, value, "date is in the future")


def check_schema(adapter: str, model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        field = ".".join(str(x) for x in err["loc"]) or "<root>"
        raise DataQualityError(adapter, field, err.get("input"), err["msg"]) from e


def parse_decimal(adapter: str, field: str, value: Any) -> Decimal:
    """Decimal from text such as "1,23,456.50" (thousands separators stripped); else DataQuality."""
    text = str(value).replace(",", "").strip()
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise DataQualityError(adapter, field, value, "not a number") from None
    if not d.is_finite():
        raise DataQualityError(adapter, field, value, "not a finite number")
    return d
