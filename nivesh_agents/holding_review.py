"""Review of one held security against its thesis (ST-8.2).

Code computes everything it can before the reviewer is asked: the machine kill criteria, the six
section 15.6 rules, the weights and the tax note. The reviewer judges the text-only criteria,
writes reasons and proposes an action. Then code merges the criteria (a code value wins), re-runs
the rules, and applies `action_floor`, which can only lower the proposal. Triggers, the tax note
and the overrides in the saved review are always code's, never the model's.
"""

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import date

from pydantic import BaseModel

from nivesh_agents.analysts import Target
from nivesh_agents.context import RunCtx
from nivesh_agents.runtime import ToolCtx
from nivesh_agents.schemas import CriterionCheck, HoldingReview, Reason, TriggerResult
from nivesh_agents.specs import SPECS
from nivesh_agents.store import RunStore
from nivesh_core.review_config import ReviewSettings
from nivesh_core.thesis import Thesis
from nivesh_engine.review_rules import (
    ReviewFacts,
    Status,
    action_floor,
    criterion_status,
    evaluate_triggers,
    merge_criterion,
)

UNAVAILABLE = "reviewer_unavailable"


@dataclass(frozen=True)
class ReviewItem:
    """One holding to review: identifiers, its active thesis and the code-computed facts."""

    target: Target
    thesis: Thesis
    facts: ReviewFacts


def _prompt(item: ReviewItem, as_of: date, cfg: ReviewSettings) -> str:
    """Identifiers, the thesis and code results only: no quantities, values or lot units."""
    t, f = item.thesis, item.facts
    pre = tuple(
        (c.criterion_id, criterion_status(c, f.metrics.get(c.metric) if c.metric else None))
        for c in t.kill_criteria
    )
    triggers = evaluate_triggers(replace(f.triggers, criteria=pre), cfg, horizon=t.horizon)
    criteria = [
        {**c.model_dump(mode="json"), "code_status": s if c.machine else "judged by you"}
        for c, (_, s) in zip(t.kill_criteria, pre, strict=True)
    ]
    body = {
        "task": "Review this holding against its thesis. Call the engine tools for evidence.",
        "as_of": as_of.isoformat(), "security_id": item.target.security_id,
        "symbol": item.target.symbol, "name": item.target.name, "market": item.target.market,
        "thesis": {"horizon": t.horizon, "why": t.why, "created_at": t.created_at.isoformat(),
                   "review_date": t.review_date.isoformat(), "kill_criteria": criteria},
        "triggers": [{"code": x.code, "status": x.status, "detail": x.detail} for x in triggers],
        "position_weight_pct": _s(f.triggers.position_weight_pct),
        "sector_weight_pct": _s(f.triggers.sector_weight_pct),
        "tax_note": f.tax_note, "review_due": t.review_date <= as_of,
    }  # fmt: skip
    return json.dumps(body, sort_keys=True)


def _s(x: object) -> str | None:
    return None if x is None else str(x)


def finalise_review(
    item: ReviewItem, as_of: date, cfg: ReviewSettings, proposal: HoldingReview | None,
    reason: str | None,
) -> HoldingReview:  # fmt: skip
    """The sole producer of a saved review: criteria merged, rules re-run in code, the proposal
    (REVIEW with low confidence when there is none) lowered by `action_floor`, and the
    code-owned fields filled."""
    t, f = item.thesis, item.facts
    sid = item.target.security_id
    given = {c.criterion_id: c for c in (proposal.criteria if proposal else [])}
    notes: list[str] = []
    checks: list[CriterionCheck] = []
    merged: list[tuple[int, Status]] = []
    for c in t.kill_criteria:
        agent = given.get(c.criterion_id)
        code_value = f.metrics.get(c.metric) if c.metric else None
        status, note = merge_criterion(c, code_value, agent.status if agent else "unknown")
        notes += [note] if note else []
        by_code = c.machine and criterion_status(c, code_value) != "unknown"
        checks.append(
            CriterionCheck(
                criterion_id=c.criterion_id,
                status=status,
                evidence=[] if by_code or agent is None else list(agent.evidence),
                judged_by="code" if by_code else "agent",
            )  # fmt: skip
        )
        merged.append((c.criterion_id, status))
    rules = evaluate_triggers(replace(f.triggers, criteria=tuple(merged)), cfg, horizon=t.horizon)
    if proposal is not None:
        replaced = [
            name
            for name, v in (("triggers", proposal.triggers), ("tax_note", proposal.tax_note),
                            ("overrides", proposal.overrides))
            if v
        ]  # fmt: skip
        if replaced:
            notes.append(f"agent-supplied {', '.join(replaced)} replaced by code")
    unknown = [
        f"criterion {c.criterion_id} metric {c.metric} unknown to engines"
        for c in t.kill_criteria
        if c.metric is not None and c.metric not in f.metrics
    ]
    notes += unknown
    proposed = proposal.action if proposal else "REVIEW"
    override = proposal.override_reason if proposal else ""
    action, floor_notes = action_floor(
        proposed, merged, rules, override_reason=override, review_due=t.review_date <= as_of,
        soft_reasons=unknown,
    )  # fmt: skip
    tax = f.tax_note + (f" | {f.trim_note}" if action == "TRIM" and f.trim_note else "")
    code_fields = {
        "action": action, "criteria": checks, "tax_note": tax,
        "triggers": [TriggerResult(code=x.code, status=x.status, detail=x.detail) for x in rules],  # type: ignore[arg-type]
        "overrides": [*notes, *floor_notes],
    }  # fmt: skip
    if proposal is None:
        return HoldingReview(
            security_id=sid, as_of=as_of, confidence="low", thesis_status="unknown",
            reasons=[Reason(code=UNAVAILABLE, text=(reason or "reviewer failed")[:600])],
            **code_fields,  # type: ignore[arg-type]
        )  # fmt: skip
    return proposal.model_copy(update=code_fields)


async def review_holding(
    run: RunCtx, item: ReviewItem, cfg: ReviewSettings, store: RunStore
) -> HoldingReview:
    """One reviewer call (its own engine calls are the only valid evidence), then the code
    floor; the final review is saved to the run directory."""
    sid, ids = item.target.security_id, {c.criterion_id for c in item.thesis.kill_criteria}

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if not isinstance(model, HoldingReview):
            return ["unexpected output type"]
        errors = []
        if model.security_id != sid:
            errors.append(f"security_id must be {sid}")
        if model.as_of != run.as_of:
            errors.append(f"as_of must be {run.as_of.isoformat()}")
        given = [c.criterion_id for c in model.criteria]
        unknown = sorted(set(given) - ids)
        if unknown:
            errors.append(f"criteria {unknown} are not in the thesis")
        if len(set(given)) != len(given):
            errors.append("duplicate criterion_id in criteria")
        if any(c.judged_by != "agent" for c in model.criteria):
            errors.append("judged_by must be agent: code fills its own checks")
        return errors

    res = await run.run(
        SPECS["holding_review"], _prompt(item, run.as_of, cfg), security_id=sid, extra_check=check
    )
    proposal = res.output if isinstance(res.output, HoldingReview) else None
    review = finalise_review(item, run.as_of, cfg, proposal, res.reason)
    store.output("holding_review", sid, review.model_dump(mode="json"))
    return review


async def review_all(
    run: RunCtx, items: list[ReviewItem], cfg: ReviewSettings, store: RunStore
) -> list[HoldingReview]:
    """Every holding concurrently under the run's slots; a crash still yields a floored review."""
    done = await asyncio.gather(
        *(review_holding(run, i, cfg, store) for i in items), return_exceptions=True
    )
    out = []
    for item, r in zip(items, done, strict=True):
        if isinstance(r, BaseException):
            r = finalise_review(item, run.as_of, cfg, None, type(r).__name__)
            store.output("holding_review", item.target.security_id, r.model_dump(mode="json"))
        out.append(r)
    return out
