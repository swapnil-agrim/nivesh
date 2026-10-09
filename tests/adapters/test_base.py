from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from nivesh_adapters.base import Adapter, AdapterResult
from nivesh_adapters.quality import DataQualityError

AS_OF = datetime(2026, 1, 2, tzinfo=UTC)


class Dummy(Adapter):
    name = "dummy"
    source = "dummy-feed"

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        return {"price": params["price"]}, AS_OF

    def validate(self, data: Any) -> None:
        if data["price"] < 0:
            raise DataQualityError(self.name, "price", data["price"], "negative")


def test_result_carries_metadata() -> None:
    r = Dummy().fetch(price=5)
    assert r.data == {"price": 5}
    assert r.source == "dummy-feed"
    assert r.as_of == AS_OF
    assert r.fetched_at.utcoffset() is not None and r.fetched_at.tzinfo is UTC
    assert r.stale is False


def test_validation_runs_before_returning() -> None:
    with pytest.raises(DataQualityError) as ei:
        Dummy().fetch(price=-1)
    assert ei.value.adapter == "dummy" and ei.value.field == "price"


def test_result_rejects_missing_source_and_naive_dates() -> None:
    ok = {"data": 1, "source": "s", "as_of": AS_OF, "fetched_at": AS_OF}
    AdapterResult(**ok)
    with pytest.raises(ValidationError):
        AdapterResult(**{**ok, "source": ""})
    with pytest.raises(ValidationError):
        AdapterResult(**{k: v for k, v in ok.items() if k != "source"})
    with pytest.raises(ValidationError):
        AdapterResult(**{**ok, "as_of": datetime(2026, 1, 1)})  # noqa: DTZ001
    with pytest.raises(ValidationError):
        AdapterResult(**{**ok, "fetched_at": datetime(2026, 1, 1)})  # noqa: DTZ001


def test_result_is_frozen() -> None:
    r = Dummy().fetch(price=1)
    with pytest.raises(ValidationError):
        r.source = "x"  # type: ignore[misc]


def test_naive_as_of_from_adapter_is_rejected() -> None:
    class Naive(Dummy):
        def _fetch(self, **params: Any) -> tuple[Any, datetime]:
            return {"price": 1}, datetime(2026, 1, 1)  # noqa: DTZ001

    with pytest.raises(ValidationError):
        Naive().fetch(price=1)
