from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nivesh_core.yamlio import read_mapping, validate


def _sums_to_100(d: dict[str, float]) -> dict[str, float]:
    if abs(sum(d.values()) - 100) > 1e-6:
        raise ValueError("values must sum to 100")
    return d


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_tolerance: Literal["conservative", "moderate", "aggressive"]
    horizon_split: dict[str, float]
    target_allocation: dict[str, float]
    max_position_pct: float = Field(default=10, gt=0, le=100)
    max_sector_pct: float = Field(default=30, gt=0, le=100)
    exclusions: list[str] = []
    tax_rates: dict[str, float] = {}
    base_currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    monthly_cost_cap: float = Field(ge=0)

    _check_horizon = field_validator("horizon_split")(_sums_to_100)
    _check_alloc = field_validator("target_allocation")(_sums_to_100)


def load_profile(path: Path) -> Profile:
    return validate(Profile, read_mapping(path), path)
