"""Output contracts of the research committee (ST-7.1) and portfolio review (ST-8.1, ST-8.2). The
pydantic models here are the single source; `schemas/<Name>.json` is generated from them and a
drift test guards the committed files.

Regenerate the nine files (run from the repository root):

    uv run python -c "from nivesh_agents.schemas import write_schema_files; write_schema_files()"

Validation is `Model.model_validate_json` (strict JSON mode, unknown fields rejected).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nivesh_core.thesis import Horizon, KillCriterion, check_criteria, check_why

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
Stance = Literal["bullish", "neutral", "bearish", "insufficient_data"]
Confidence = Literal["low", "medium", "high"]
LADDER: tuple[str, ...] = (
    "BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL", "AVOID", "INSUFFICIENT_DATA",
)  # fmt: skip
VerdictLabel = Literal["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL", "AVOID", "INSUFFICIENT_DATA"]
Value = int | float | str


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _words(text: str, limit: int, what: str) -> str:
    if len(text.split()) > limit:
        raise ValueError(f"{what} must be at most {limit} words")
    return text


class Evidence(_M):
    tool_call_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: Value
    as_of: date | None = None
    source_url: str | None = None  # news evidence only


class KeyPoint(_M):
    claim: str = Field(min_length=1, max_length=600)
    evidence: list[Evidence] = Field(min_length=1)


class Levels(_M):
    """Technical numbers; each must equal the engine value (checked outside the schema)."""

    setup_type: str | None = None
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    stop: Decimal | None = None
    invalidation: Decimal | None = None


class AnalystView(_M):
    schema_version: Literal[1] = 1
    agent: Literal["fundamental", "technical", "news"]
    security_id: int
    as_of: date
    stance: Stance
    score: int = Field(ge=0, le=100)
    confidence: Confidence
    thesis: str
    key_points: list[KeyPoint] = []
    risks: list[str] = []
    data_gaps: list[str] = []
    levels: Levels | None = None

    @field_validator("thesis")
    @classmethod
    def _thesis(cls, v: str) -> str:
        return _words(v, 120, "thesis")

    @model_validator(mode="after")
    def _rules(self) -> "AnalystView":
        if self.stance == "insufficient_data" and not self.data_gaps:
            raise ValueError("insufficient_data stance needs data_gaps")
        if self.agent == "fundamental" and self.stance != "insufficient_data":
            if len(self.key_points) < 3:
                raise ValueError("fundamental view needs at least 3 key_points")
        if self.levels is not None and self.agent != "technical":
            raise ValueError("levels belong to the technical agent")
        return self


class SectorTilt(_M):
    sector: str
    tilt: Literal["overweight", "neutral", "underweight"]
    evidence: list[Evidence] = Field(min_length=1)


class MacroView(_M):
    schema_version: Literal[1] = 1
    agent: Literal["macro"] = "macro"
    as_of: date
    regime: Literal["risk_on", "neutral", "risk_off", "insufficient_data"]
    score: int = Field(ge=0, le=100)
    confidence: Confidence
    thesis: str
    key_points: list[KeyPoint] = []
    sector_tilts: list[SectorTilt] = []
    risks: list[str] = []
    data_gaps: list[str] = []

    @field_validator("thesis")
    @classmethod
    def _thesis(cls, v: str) -> str:
        return _words(v, 120, "thesis")

    @model_validator(mode="after")
    def _rules(self) -> "MacroView":
        if self.regime == "insufficient_data" and not self.data_gaps:
            raise ValueError("insufficient_data regime needs data_gaps")
        return self


class FundView(_M):
    schema_version: Literal[1] = 1
    agent: Literal["mf"] = "mf"
    security_id: int
    scheme: str = Field(min_length=1)  # AMFI code
    as_of: date
    stance: Stance
    score: int = Field(ge=0, le=100)
    confidence: Confidence
    thesis: str
    key_points: list[KeyPoint] = []
    cost_note: str = ""
    overlap_note: str = ""
    category_fit: str = ""
    action: Literal["keep", "add", "trim", "exit", "switch", "review"]
    switch_target: str | None = None
    risks: list[str] = []
    data_gaps: list[str] = []

    @field_validator("thesis")
    @classmethod
    def _thesis(cls, v: str) -> str:
        return _words(v, 120, "thesis")

    @model_validator(mode="after")
    def _rules(self) -> "FundView":
        if self.stance == "insufficient_data" and not self.data_gaps:
            raise ValueError("insufficient_data stance needs data_gaps")
        return self


class ViewRef(_M):
    """Pointer into an analyst view that the agent was given."""

    view: str
    point_index: int = Field(ge=0)


class ToolRef(_M):
    tool_call_id: str = Field(min_length=1)


Ref = ViewRef | ToolRef


class Claim(_M):
    claim: str = Field(min_length=1, max_length=600)
    evidence: list[Ref] = Field(min_length=1)


class DebateTurn(_M):
    schema_version: Literal[1] = 1
    security_id: int
    round: int = Field(ge=1)
    side: Literal["bull", "bear"]
    claims: list[Claim] = Field(min_length=1)
    rebuts: list[int] = []  # indices into the opponent's previous claims
    strongest_unrebutted: str | None = None
    summary: bool = False


class LensView(_M):
    schema_version: Literal[1] = 1
    lens: Literal["value", "growth", "contrarian", "valuation"]
    security_id: int
    decision: Literal["would_buy", "would_pass"]
    paragraph: str
    evidence: list[Ref] = Field(min_length=1)

    @field_validator("paragraph")
    @classmethod
    def _paragraph(cls, v: str) -> str:
        return _words(v, 150, "paragraph")


class WeightBounds(_M):
    min_pct: Decimal
    max_pct: Decimal


class RiskAssessment(_M):
    schema_version: Literal[1] = 1
    security_id: int
    veto: bool
    veto_reasons: list[str] = []
    weight_bounds: WeightBounds
    liquidity_note: str = ""
    correlation_note: str = ""
    concentration_note: str = ""
    risks: list[str] = []
    overrides: list[str] = []


class Reason(_M):
    code: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=600)


class CriterionCheck(_M):
    """One kill criterion: met, not_met or unknown. An agent's met or not_met needs evidence;
    code-judged entries (machine metrics) carry `judged_by: code`."""

    criterion_id: int = Field(ge=1)
    status: Literal["met", "not_met", "unknown"]
    evidence: list[Evidence] = []
    judged_by: Literal["agent", "code"] = "agent"

    @model_validator(mode="after")
    def _evidence(self) -> "CriterionCheck":
        if self.status != "unknown" and self.judged_by == "agent" and not self.evidence:
            raise ValueError("a met or not_met criterion needs evidence")
        return self


TriggerCode = Literal[
    "kill_criterion", "fundamental_deterioration", "valuation_stretch", "concentration",
    "below_sma200_weak_rs", "better_use_of_capital",
]  # fmt: skip


class TriggerResult(_M):
    """A section 15.6 rule result, computed in code."""

    code: TriggerCode
    status: Literal["triggered", "clear", "not_evaluable", "not_applicable"]
    detail: str = ""


class HoldingReview(_M):
    """Review of one held security against its thesis. Triggers, the tax note and overrides
    are filled by code; code can only lower the proposed action."""

    schema_version: Literal[2] = 2
    security_id: int
    as_of: date
    action: Literal["HOLD", "ADD", "TRIM", "EXIT", "REVIEW"]
    confidence: Confidence
    reasons: list[Reason] = Field(min_length=1)
    criteria: list[CriterionCheck] = []
    triggers: list[TriggerResult] = []
    thesis_status: Literal["intact", "weakened", "broken", "unknown"]
    valuation_stretch: bool | None = None
    tax_note: str = ""
    override_reason: str = ""
    overrides: list[str] = []
    evidence: list[Evidence] = []


class ThesisDraft(_M):
    """A drafted thesis for the owner to accept, edit or skip (ST-8.1)."""

    schema_version: Literal[1] = 1
    security_id: int
    as_of: date
    horizon: Horizon
    why: str
    kill_criteria: list[KillCriterion]
    evidence: list[ViewRef] = Field(min_length=1)
    data_gaps: list[str] = []

    @field_validator("why")
    @classmethod
    def _why(cls, v: str) -> str:
        return check_why(v)

    @field_validator("kill_criteria")
    @classmethod
    def _criteria(cls, v: list[KillCriterion]) -> list[KillCriterion]:
        return check_criteria(v)


class EntryZone(_M):
    low: Decimal
    high: Decimal
    ccy: Literal["INR", "USD"]


class CommitteeVerdict(_M):
    model_config = ConfigDict(extra="forbid", strict=True, title="Verdict")
    schema_version: Literal[1] = 1
    security_id: int
    as_of: date
    verdict: VerdictLabel
    horizon: Horizon
    conviction: Confidence
    suggested_weight_pct: Decimal | None = None
    entry_zone: EntryZone | None = None
    invalidation: list[str] = []
    review_date: date
    bull_case: str
    bear_case: str
    risk_notes: str
    vetoed_by_risk: bool
    coverage_pct: Decimal
    overrides: list[str] = []


MODELS: dict[str, type[BaseModel]] = {
    "AnalystView": AnalystView,
    "MacroView": MacroView,
    "FundView": FundView,
    "DebateTurn": DebateTurn,
    "LensView": LensView,
    "RiskAssessment": RiskAssessment,
    "HoldingReview": HoldingReview,
    "Verdict": CommitteeVerdict,
    "ThesisDraft": ThesisDraft,
}


def schema_json(name: str) -> dict[str, Any]:
    return MODELS[name].model_json_schema()


def schema_text(name: str) -> str:
    return json.dumps(schema_json(name), indent=2, sort_keys=True) + "\n"


def write_schema_files(directory: Path = SCHEMA_DIR) -> None:
    directory.mkdir(exist_ok=True)
    for name in MODELS:
        (directory / f"{name}.json").write_text(schema_text(name))
