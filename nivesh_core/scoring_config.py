"""Owner-set parameters of ledger scoring and evaluation (ST-12.2 to ST-12.7): the `scoring:`
and `evaluation:` blocks. Every number the scorer uses is a parameter here with a documented
default; none is advice. Numbers are Decimal, unknown keys are rejected, and no field name looks
sensitive to the redaction helper.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HORIZONS = (1, 4, 13, 26, 52)  # weeks; the ledger_score table allows exactly these


class ScoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    horizons_weeks: tuple[int, ...] = HORIZONS
    # assumed round-trip cost in percent, by market; applied to BUY and ACCUMULATE calls only
    cost_pct: dict[str, Decimal] = {"IN": Decimal("0.25"), "US": Decimal("0.10")}
    benchmarks: dict[str, str] = {}  # market -> index symbol; empty: analysis.ta.benchmarks
    stale_days: int = Field(default=7, ge=0)  # a leg whose last bar is older is insufficient
    min_bucket: int = Field(default=20, ge=1)  # scorecard: fewer scored calls is not enough data
    calibration_min: int = Field(default=40, ge=1)
    resamples: int = Field(default=1000, ge=1)
    resample_seed: int = Field(default=20260101, ge=1)  # nonzero: a zero state never moves
    primary_horizon_weeks: int = 13

    @field_validator("horizons_weeks")
    @classmethod
    def _horizons(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v or not set(v) <= set(HORIZONS) or len(set(v)) != len(v):
            raise ValueError(f"horizons_weeks must be distinct values from {HORIZONS}")
        return v

    @field_validator("cost_pct")
    @classmethod
    def _cost(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        for market, cost in v.items():
            if market not in ("IN", "US") or not cost.is_finite() or cost < 0:
                raise ValueError("cost_pct needs IN and US keys with zero or more percent")
        return v

    @model_validator(mode="after")
    def _primary(self) -> "ScoringSettings":
        if self.primary_horizon_weeks not in self.horizons_weeks:
            raise ValueError("primary_horizon_weeks must be one of horizons_weeks")
        return self


class EvaluationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # model id -> last date of its training data; used to refuse in-sample historical evaluation
    model_cutoffs: dict[str, date] = {}
