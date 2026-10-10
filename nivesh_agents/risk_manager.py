"""The risk manager (ST-7.8). The model writes notes; the veto and the weight bounds are computed
in code from the profile limits and the engine's facts, and overwrite whatever the model drafted.
A failed risk agent is a veto (`risk_unavailable`), so the portfolio manager cannot go upward.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel

from nivesh_agents.analysts import Target, analyst_prompt
from nivesh_agents.context import RunCtx
from nivesh_agents.runtime import AgentResult, ToolCtx
from nivesh_agents.schemas import RiskAssessment, WeightBounds
from nivesh_agents.specs import SPECS
from nivesh_engine.committee_rules import Bounds, RiskFacts, Veto, risk_veto, weight_bounds

UNAVAILABLE = "risk_unavailable"


@dataclass(frozen=True)
class RiskOutcome:
    assessment: RiskAssessment
    veto: Veto
    bounds: Bounds
    result: AgentResult


def facts_json(facts: RiskFacts) -> dict[str, object]:
    return {
        "excluded": facts.excluded, "hard_red_flag": facts.hard_flag,
        "position_over_limit": facts.position_over_limit,
        "sector_over_limit": facts.sector_over_limit,
        "days_to_trade": None if facts.days_to_trade is None else str(facts.days_to_trade),
        "profile_max_position_pct": str(facts.max_position_pct),
        "profile_max_sector_pct": str(facts.max_sector_pct),
        "sector_headroom_pct": (
            None if facts.sector_headroom_pct is None else str(facts.sector_headroom_pct)
        ),
        "tested_weight_pct": str(facts.tested_weight_pct),
    }  # fmt: skip


def _default_notes(facts: RiskFacts) -> tuple[str, str]:
    dtt = facts.days_to_trade
    liquidity = (
        f"Days to trade the tested position: {dtt}."
        if dtt is not None
        else "Days to trade could not be computed from stored bars."
    )
    concentration = (
        f"Tested weight {facts.tested_weight_pct}% against a profile position limit of "
        f"{facts.max_position_pct}% and a sector limit of {facts.max_sector_pct}%."
    )
    return liquidity, concentration


def merge_assessment(
    draft: RiskAssessment | None, facts: RiskFacts, veto: Veto, bounds: Bounds,
    *, reason: str = UNAVAILABLE, fail_closed: bool = True,
) -> RiskAssessment:  # fmt: skip
    """Code fields over the model draft; every difference is listed in `overrides`. With no
    draft the assessment is a veto (`fail_closed`), unless the agent was deliberately not run."""
    liquidity, concentration = _default_notes(facts)
    reasons = list(veto.reasons)
    overrides: list[str] = []
    risks: list[str] = []
    correlation = "Correlation was not assessed."
    sec_id = 0
    if draft is not None:
        sec_id = draft.security_id
        risks = list(draft.risks)
        correlation = draft.correlation_note or correlation
        liquidity = draft.liquidity_note or liquidity
        concentration = draft.concentration_note or concentration
        if draft.veto != veto.veto:
            overrides.append(f"veto: model said {draft.veto}, rules say {veto.veto}")
        if draft.weight_bounds.max_pct != bounds.max_pct:
            overrides.append(
                f"weight_bounds.max_pct {draft.weight_bounds.max_pct} -> {bounds.max_pct}"
            )
        overrides += list(draft.overrides)
    elif fail_closed:
        reasons.insert(0, reason)
        overrides.append("risk agent produced no valid output; treated as a veto")
    else:
        overrides.append(f"risk agent not run: {reason}")
    return RiskAssessment(
        security_id=sec_id, veto=veto.veto or (draft is None and fail_closed), veto_reasons=reasons,
        weight_bounds=WeightBounds(min_pct=bounds.min_pct, max_pct=bounds.max_pct),
        liquidity_note=liquidity, correlation_note=correlation, concentration_note=concentration,
        risks=risks, overrides=overrides,
    )  # fmt: skip


async def run_risk(
    run: RunCtx, target: Target, facts: RiskFacts, *, max_days_to_trade: Decimal | None
) -> RiskOutcome:
    veto = risk_veto(facts, max_days_to_trade=max_days_to_trade)
    bounds = weight_bounds(facts)
    body = json.loads(analyst_prompt(run, target, "Assess sizing, liquidity, correlation."))
    body["facts"] = facts_json(facts)
    body["hint"] = (
        f"Call risk_metrics with candidate {target.symbol} and weight_pct "
        f"{facts.tested_weight_pct}, and portfolio_xray for concentration."
    )

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if getattr(model, "security_id", target.security_id) != target.security_id:
            return [f"security_id must be {target.security_id}"]
        return []

    res = await run.run(
        SPECS["risk"], json.dumps(body, sort_keys=True), security_id=target.security_id,
        extra_check=check,
    )  # fmt: skip
    draft = res.output if isinstance(res.output, RiskAssessment) else None
    merged = merge_assessment(draft, facts, veto, bounds)
    merged = merged.model_copy(update={"security_id": target.security_id})
    return RiskOutcome(merged, Veto(merged.veto, tuple(merged.veto_reasons)), bounds, res)
