"""Thesis drafting for onboarding (ST-8.1): the analysts first, then one drafting call with no
tools. The drafter sees the analyst views and the security's identifiers only (no quantities,
values or holder refs), and every pointer it cites must exist in those views. Analyst views and
drafts are saved to the run directory before the owner is asked; the owner then accepts, edits
or skips each draft, and only an accepted or a valid edited draft becomes a Thesis.
"""

import asyncio
import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from nivesh_agents.analysts import QUICK_ANALYSTS, Target, run_analyst
from nivesh_agents.context import RunCtx
from nivesh_agents.debate import pointer_errors, views_payload
from nivesh_agents.runtime import ToolCtx
from nivesh_agents.schemas import ThesisDraft
from nivesh_agents.specs import SPECS
from nivesh_agents.store import RunStore
from nivesh_core.thesis import KillCriterion, Thesis, default_review_date
from nivesh_engine.metrics import known_metrics

THESIS_CLASSES = ("equity", "etf")
Choice = Literal["accept", "edit", "skip"]


@dataclass(frozen=True)
class Held:
    """One held security (a row per account and holder until merged)."""

    security_id: int
    symbol: str
    asset_class: str
    value_inr: Decimal | None
    name: str = ""
    market: str = "IN"


@dataclass(frozen=True)
class DraftOutcome:
    target: Target
    draft: ThesisDraft | None
    reason: str | None = None  # why the holding was skipped
    gaps: tuple[str, ...] = ()  # analysts that gave no view


def merge_held(rows: Iterable[Held]) -> list[Held]:
    """Equity and ETF rows merged per security (values summed; None when any is unknown),
    largest INR value first."""
    merged: dict[int, Held] = {}
    for h in rows:
        if h.asset_class not in THESIS_CLASSES:
            continue
        prev = merged.get(h.security_id)
        if prev is not None:
            total = (
                None
                if prev.value_inr is None or h.value_inr is None
                else prev.value_inr + h.value_inr
            )
            h = replace(prev, value_inr=total)
        merged[h.security_id] = h
    return sorted(merged.values(), key=lambda x: (-(x.value_inr or Decimal(0)), x.symbol))


def onboard_targets(held: Iterable[Held], covered: Collection[int]) -> list[Target]:
    """Held equity and ETF securities without an active thesis, largest first."""
    return [
        Target(h.security_id, h.symbol, h.name or h.symbol, h.asset_class, h.market)
        for h in merge_held(held)
        if h.security_id not in covered
    ]


def metric_errors(criteria: Iterable[KillCriterion]) -> list[str]:
    """A machine criterion must name a metric the engines produce, else it is never evaluated."""
    known = known_metrics()
    return [
        f"criterion {c.criterion_id}: {c.metric} is not an engine metric"
        for c in criteria
        if c.metric is not None and c.metric not in known
    ]


def _prompt(run: RunCtx, target: Target, views: dict[str, BaseModel], gaps: list[str]) -> str:
    body = {
        "task": "Draft the thesis from the analyst views only. Use no tools.",
        "as_of": run.as_of.isoformat(), "security_id": target.security_id,
        "symbol": target.symbol, "name": target.name, "kind": target.kind,
        "market": target.market, "views": views_payload(views), "data_gaps": gaps,
    }  # fmt: skip
    return json.dumps(body, sort_keys=True)


async def draft_thesis(run: RunCtx, target: Target, store: RunStore) -> DraftOutcome:
    """Analysts concurrently, then one drafting call; every output is saved before returning."""
    sid = target.security_id
    raw = await asyncio.gather(
        *(run_analyst(run, n, target) for n in QUICK_ANALYSTS), return_exceptions=True
    )
    views: dict[str, BaseModel] = {}
    gaps: list[str] = []
    for name, res in zip(QUICK_ANALYSTS, raw, strict=True):
        if isinstance(res, BaseException):
            gaps.append(f"{name}: {type(res).__name__}")
            store.failed(name, sid, type(res).__name__)
        elif isinstance(res.output, BaseModel):
            views[name] = res.output
            store.output(name, sid, res.output.model_dump(mode="json"))
        else:
            gaps.append(f"{name}: {res.reason}")
            store.failed(name, sid, res.reason or "failed")
    if not views:
        reason = "no analyst view: " + "; ".join(gaps)
        store.failed("thesis_draft", sid, reason)
        return DraftOutcome(target, None, reason, tuple(gaps))

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if not isinstance(model, ThesisDraft):
            return ["unexpected output type"]
        errors = pointer_errors(list(model.evidence), views) + metric_errors(model.kill_criteria)
        if model.security_id != sid:
            errors.append(f"security_id must be {sid}")
        if model.as_of != run.as_of:
            errors.append(f"as_of must be {run.as_of.isoformat()}")
        return errors

    res = await run.run(
        SPECS["thesis_draft"], _prompt(run, target, views, gaps), security_id=sid,
        extra_check=check, evidence_ids=frozenset(),
    )  # fmt: skip
    if not isinstance(res.output, ThesisDraft):
        reason = f"draft failed: {res.reason}"
        store.failed("thesis_draft", sid, reason)
        return DraftOutcome(target, None, reason, tuple(gaps))
    store.output("thesis_draft", sid, res.output.model_dump(mode="json"))
    return DraftOutcome(target, res.output, None, tuple(gaps))


async def draft_all(run: RunCtx, targets: list[Target], store: RunStore) -> list[DraftOutcome]:
    """One draft per target, concurrently under the run's slots; a crash skips that target."""
    done = await asyncio.gather(
        *(draft_thesis(run, t, store) for t in targets), return_exceptions=True
    )
    return [
        r if isinstance(r, DraftOutcome) else DraftOutcome(t, None, type(r).__name__)
        for t, r in zip(targets, done, strict=True)
    ]


def apply_choice(
    draft: ThesisDraft, choice: Choice, edits: dict[str, Any] | None, *, created: date,
    review_days: int,
) -> Thesis | list[str] | None:  # fmt: skip
    """The owner's answer: None to skip, a Thesis when valid, else the validation errors."""
    if choice == "skip":
        return None
    data: dict[str, Any] = {
        "security_id": draft.security_id, "created_at": created.isoformat(),
        "horizon": draft.horizon, "why": draft.why,
        "kill_criteria": [c.model_dump(mode="json") for c in draft.kill_criteria],
        "review_date": default_review_date(created, review_days).isoformat(),
    }  # fmt: skip
    if choice == "edit":
        data.update(edits or {})
    try:
        thesis = Thesis.model_validate_json(json.dumps(data, default=str))
    except ValidationError as e:
        return [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
    return metric_errors(thesis.kill_criteria) or thesis
