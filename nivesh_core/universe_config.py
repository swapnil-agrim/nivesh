"""Owner-set parameters of idea generation (ST-9.1 to ST-9.5): the `universe:` and `ideas:`
blocks. Every number is a parameter with a documented default and none is advice. Numbers are
Decimal, unknown keys are rejected, and no field name looks like a credential.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Market = Literal["IN", "US"]


class _G(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexSpec(_G):
    market: Market
    enabled: bool = True


class IndiaLiquidity(_G):
    """Floor on the 20-day average daily value traded, in crore of INR (1 crore = 10^7)."""

    min_adv_crore: Decimal = Field(default=Decimal("5"), gt=0)


class UsLiquidity(_G):
    """Floor on the 20-day average daily value traded, in millions of USD (before any FX)."""

    min_adv_usd_m: Decimal = Field(default=Decimal("20"), gt=0)


class Liquidity(_G):
    IN: IndiaLiquidity = IndiaLiquidity()
    US: UsLiquidity = UsLiquidity()


def _default_indices() -> dict[str, IndexSpec]:
    return {
        "NIFTY500": IndexSpec(market="IN"),
        "NIFTYSMALLCAP250": IndexSpec(market="IN", enabled=False),
        "SP500": IndexSpec(market="US"),
        "NASDAQ100": IndexSpec(market="US"),
        "RUSSELL1000": IndexSpec(market="US", enabled=False),
    }


class UniverseSettings(_G):
    indices: dict[str, IndexSpec] = Field(default_factory=_default_indices)
    liquidity: Liquidity = Liquidity()
    max_age_days: int = Field(default=35, ge=1)  # older membership snapshot: a warning
    max_bar_age_days: int = Field(default=7, ge=1)  # older last bar: liquidity unknown

    @model_validator(mode="after")
    def _names(self) -> "UniverseSettings":
        for name in self.indices:
            if not name or not all(c.isupper() or c.isdigit() or c == "_" for c in name):
                raise ValueError(f"index name {name!r} must be UPPER_SNAKE letters and digits")
        return self


class IdeasSettings(_G):
    shortlist_size: int = Field(default=8, ge=1)
    shortlist_max: int = Field(default=12, ge=1)  # hard cap on committee runs per market
    sector_cap: int = Field(default=2, ge=1)
    default_n: int = Field(default=5, ge=1)
    held: Literal["label", "exclude"] = "label"
    default_preset: str = Field(default="lt-quality-value", pattern=r"^[a-z0-9-]+$")
    max_runs: int = Field(default=16, ge=1)  # committee runs per command, all markets together

    @model_validator(mode="after")
    def _within_cap(self) -> "IdeasSettings":
        if self.shortlist_size > self.shortlist_max:
            raise ValueError(
                f"shortlist_size ({self.shortlist_size}) exceeds shortlist_max "
                f"({self.shortlist_max})"
            )
        return self
