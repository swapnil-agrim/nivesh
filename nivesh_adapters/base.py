"""Adapter contract: every data source returns an AdapterResult carrying provenance.

Adapters that make HTTP calls MUST obtain their client from
`nivesh_adapters.recorder.make_client` so record/replay and the test network block apply.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nivesh_core.timeutil import utcnow


class AdapterResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: Any
    source: str = Field(min_length=1)
    as_of: datetime
    fetched_at: datetime
    stale: bool = False

    @field_validator("as_of", "fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return v


class Adapter(ABC):
    name: str
    source: str

    @abstractmethod
    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        """Return (data, as_of). The only hook a concrete adapter must implement."""

    def validate(self, data: Any) -> None:  # noqa: B027 - optional hook, default accepts all
        """Raise DataQualityError for out-of-range or malformed data."""

    def fetch(self, **params: Any) -> AdapterResult:
        data, as_of = self._fetch(**params)
        self.validate(data)
        return AdapterResult(data=data, source=self.source, as_of=as_of, fetched_at=utcnow())
