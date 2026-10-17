"""Owner-set parameters of the fundamentals providers: the `fundamentals:` block. Which providers
exist, which are enabled, their role, the vendor plan limits that the local quota ledger enforces,
and the tolerances of `fundamentals compare`. The defaults reproduce today's behaviour. Unknown
keys are rejected, a credential is only ever a `ref:NAME` reference, and no field name looks
sensitive to the redaction helper. Whether a name is a registered provider is checked when the
provider is resolved (core must not import adapters).
"""

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nivesh_core.secrets import REF_RE

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ReservePct(BaseModel):
    """Share of each window kept back from lower-priority calls (holdings keep none)."""

    model_config = ConfigDict(extra="forbid")
    watchlist: int = Field(default=10, ge=0, le=90)
    universe: int = Field(default=30, ge=0, le=90)

    @model_validator(mode="after")
    def _ordered(self) -> "ReservePct":
        if self.watchlist > self.universe:
            raise ValueError("watchlist must reserve no more than universe")
        return self


class PlanSettings(BaseModel):
    """A vendor plan as the owner knows it (nothing here is read from the vendor)."""

    model_config = ConfigDict(extra="forbid")
    daily: int | None = Field(default=None, ge=1)
    monthly: int | None = Field(default=None, ge=1)
    min_interval_s: float = Field(default=1.1, ge=0, allow_inf_nan=False)
    month_start_day: int = Field(default=1, ge=1, le=28)  # IST day the monthly window starts
    reserve_pct: ReservePct = ReservePct()

    @model_validator(mode="after")
    def _a_cap(self) -> "PlanSettings":
        if self.daily is None and self.monthly is None:
            raise ValueError("a plan must set daily, monthly or both")
        return self


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE.pattern)
    enabled: bool = True
    role: Literal["primary", "fallback"] = "primary"
    plan: PlanSettings | None = None  # set for a metered provider
    credential: str | None = None  # a "ref:NAME" reference, never a value

    @field_validator("credential")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not REF_RE.match(v):
            raise ValueError("must be a reference such as 'ref:NAME'")
        return v


class CompareSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statements_tolerance_pct: Decimal = Field(default=Decimal(1), gt=0)
    shareholding_tolerance_pp: Decimal = Field(default=Decimal("0.5"), gt=0)


def _default_providers() -> list[ProviderSpec]:
    return [
        ProviderSpec(
            name="tapetide",
            plan=PlanSettings(daily=400, monthly=5000),
            credential="ref:TAPETIDE_TOKEN",
        ),
        ProviderSpec(name="bse_xbrl", role="fallback"),
        ProviderSpec(name="upstox", enabled=False, credential="ref:UPSTOX_ANALYTICS_TOKEN"),
    ]


class FundamentalsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: list[ProviderSpec] = Field(default_factory=_default_providers)
    compare: CompareSettings = CompareSettings()

    @field_validator("providers")
    @classmethod
    def _unique(cls, v: list[ProviderSpec]) -> list[ProviderSpec]:
        names = [p.name for p in v]
        if len(set(names)) != len(names):
            raise ValueError("provider names must be unique")
        return v

    def provider(self, name: str) -> ProviderSpec | None:
        return next((p for p in self.providers if p.name == name), None)
