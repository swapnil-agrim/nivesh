"""Owner-set parameters of portfolio review (ST-8.1 to ST-8.4), one nested `review:` block.

Every number the review engines use is a parameter here with a documented default (PID 15.6,
OBJ-9); none is advice. Numbers are Decimal, unknown keys are rejected, and no field name looks
like a credential.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_review_days: int = Field(default=90, ge=1)  # thesis review date = created + this
    band_pp: Decimal = Field(default=Decimal("5"), gt=0)  # allowed drift per asset class
    turnover_limit_pct: Decimal = Field(default=Decimal("30"), gt=0, le=100)  # per proposal
    valuation_percentile_min: Decimal = Field(default=Decimal("95"), ge=0, le=100)
    valuation_metric: str = Field(default="pe", pattern=r"^[a-z_]+$")
    valuation_history_years: int = Field(default=5, ge=1)
    below_sma200_sessions: int = Field(default=60, ge=1)
    deterioration_quarters: int = Field(default=2, ge=1)
