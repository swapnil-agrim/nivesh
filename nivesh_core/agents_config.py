"""Owner-set parameters of the research committee (ST-7.1 to ST-7.9), one nested `agents:` block.

Model ids are configuration, not code (PID 14.1). The defaults are the SDK aliases and are
unverified against the live CLI; the owner sets exact ids. Numbers are Decimal, unknown keys are
rejected, and no field name looks like a credential.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AGENTS: tuple[str, ...] = (
    "fundamental", "technical", "news", "macro", "mf", "bull", "bear", "lens", "risk", "pm",
)  # fmt: skip
Tier = Literal["top", "mid", "small"]
LensName = Literal["value", "growth", "contrarian", "valuation"]
LENS_NAMES: tuple[str, ...] = ("value", "growth", "contrarian", "valuation")


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelIds(_Group):
    """Tier to model id."""

    top: str = "opus"
    mid: str = "sonnet"
    small: str = "haiku"


def _default_tiers() -> dict[str, Tier]:
    return {
        "fundamental": "top", "technical": "mid", "news": "mid", "macro": "mid", "mf": "mid",
        "bull": "top", "bear": "top", "lens": "mid", "risk": "top", "pm": "top",
    }  # fmt: skip


class AgentsSettings(_Group):
    models: ModelIds = ModelIds()
    tiers: dict[str, Tier] = Field(default_factory=_default_tiers)
    concurrency: int = Field(default=4, ge=1, le=16)
    debate_rounds: int = Field(default=2, ge=1, le=3)
    lenses_enabled: bool = True
    lenses: list[LensName] = ["value", "growth", "contrarian", "valuation"]
    min_coverage_pct: Decimal = Field(default=Decimal("70"), ge=0, le=100)
    prompt_pins: dict[str, int] = {}
    max_turns: dict[str, int] = {}  # per-agent override of the spec default
    max_budget_usd: Decimal | None = Field(default=None, gt=0)
    starter_weight_pct: Decimal = Field(default=Decimal("2"), gt=0, lt=100)
    max_days_to_trade: Decimal | None = Field(default=None, gt=0)
    max_filing_sections: int = Field(default=2, ge=0)

    @field_validator("tiers")
    @classmethod
    def _tiers(cls, v: dict[str, Tier]) -> dict[str, Tier]:
        if set(v) != set(AGENTS):
            raise ValueError(f"must name exactly: {', '.join(AGENTS)}")
        return v

    @field_validator("prompt_pins", "max_turns")
    @classmethod
    def _per_agent(cls, v: dict[str, int]) -> dict[str, int]:
        for name, n in v.items():
            if name not in AGENTS and not name.startswith("lens_"):
                raise ValueError(f"unknown agent {name!r}")
            if n < 1:
                raise ValueError("must be a positive integer")
        return v
