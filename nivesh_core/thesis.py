"""Investment thesis entity (PID section 13, ST-8.1): why the owner holds a security and the
measurable conditions that would break that reason. Word and count limits are PID content rules,
kept as constants here rather than config. Dates are parameters; nothing reads a clock.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WHY_MAX_WORDS = 60
CRITERION_MAX_WORDS = 40
MIN_CRITERIA = 2
MAX_CRITERIA = 4
Horizon = Literal["positional_1_6m", "long_term_1y_plus"]
Comparator = Literal["lt", "lte", "gt", "gte"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _text(v: str, limit: int, what: str) -> str:
    n = len(v.split())
    if n == 0:
        raise ValueError(f"{what} must not be empty")
    if n > limit:
        raise ValueError(f"{what} must be at most {limit} words")
    return v


class KillCriterion(_M):
    """One condition that breaks the thesis. Machine-checkable when metric, comparator and
    threshold are all set (code judges it); otherwise text only (the reviewer judges it)."""

    criterion_id: int = Field(ge=1)
    text: str
    metric: str | None = None
    comparator: Comparator | None = None
    threshold: Decimal | None = None
    unit: str = ""

    @field_validator("text")
    @classmethod
    def _words(cls, v: str) -> str:
        return _text(v, CRITERION_MAX_WORDS, "criterion text")

    @model_validator(mode="after")
    def _triple(self) -> "KillCriterion":
        parts = (self.metric, self.comparator, self.threshold)
        if any(p is None for p in parts) and any(p is not None for p in parts):
            raise ValueError("metric, comparator and threshold must be set all or none")
        return self

    @property
    def machine(self) -> bool:
        return self.metric is not None


def check_criteria(criteria: list[KillCriterion]) -> list[KillCriterion]:
    """Count, unique ids and at least one machine-checkable criterion (shared with ThesisDraft)."""
    if not MIN_CRITERIA <= len(criteria) <= MAX_CRITERIA:
        raise ValueError(f"kill_criteria must hold {MIN_CRITERIA} to {MAX_CRITERIA} items")
    ids = [c.criterion_id for c in criteria]
    if len(set(ids)) != len(ids):
        raise ValueError("criterion_id values must be unique")
    if not any(c.machine for c in criteria):
        raise ValueError("at least one kill criterion must be machine-checkable")
    return criteria


def check_why(v: str) -> str:
    return _text(v, WHY_MAX_WORDS, "why")


class Thesis(_M):
    thesis_id: int | None = None
    security_id: int
    created_at: date
    horizon: Horizon
    why: str
    kill_criteria: list[KillCriterion]
    review_date: date
    status: Literal["active", "superseded", "closed"] = "active"
    source: Literal["onboarding", "decision"] = "onboarding"

    @field_validator("why")
    @classmethod
    def _why(cls, v: str) -> str:
        return check_why(v)

    @field_validator("kill_criteria")
    @classmethod
    def _crit(cls, v: list[KillCriterion]) -> list[KillCriterion]:
        return check_criteria(v)


def default_review_date(created: date, days: int) -> date:
    return created + timedelta(days=days)
