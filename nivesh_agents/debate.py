"""Bull and bear debate (ST-7.6). The two researchers have no tools: they argue only from the
analyst views they are given, and every pointer they cite must exist in those views (or be a
tool-use id the analysts really made). Quick mode is one summary each; deep mode alternates for
`debate_rounds` rounds and the last turn of each side states its strongest unrebutted point.
"""

import asyncio
import json
from dataclasses import dataclass, field

from pydantic import BaseModel

from nivesh_agents.analysts import Target
from nivesh_agents.context import RunCtx
from nivesh_agents.runtime import AgentResult, ToolCtx
from nivesh_agents.schemas import DebateTurn, ViewRef
from nivesh_agents.specs import SPECS


@dataclass
class Debate:
    turns: list[DebateTurn] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    results: list[AgentResult] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.failures)

    def strongest(self, side: str) -> str | None:
        for t in reversed(self.turns):
            if t.side == side and t.strongest_unrebutted:
                return t.strongest_unrebutted
        return None


def views_payload(views: dict[str, BaseModel]) -> dict[str, object]:
    return {name: v.model_dump(mode="json") for name, v in sorted(views.items())}


def analyst_tool_ids(run: RunCtx, target: Target, views: dict[str, BaseModel]) -> frozenset[str]:
    """Tool-use ids the analysts behind these views made (macro is shared across securities)."""
    ids: set[str] = set()
    for name in views:
        ids |= run.tracer.tool_ids(name, None if name == "macro" else target.security_id)
    return frozenset(ids)


def pointer_errors(refs: list[object], views: dict[str, BaseModel]) -> list[str]:
    """Each view pointer must name a given view and an existing key point of it."""
    errors = []
    for r in refs:
        if isinstance(r, ViewRef):
            v = views.get(r.view)
            points = getattr(v, "key_points", None)
            if points is None or r.point_index >= len(points):
                errors.append(f"pointer {r.view}[{r.point_index}] does not exist in the views")
    return errors


def _turn_refs(turn: DebateTurn) -> list[object]:
    return [e for c in turn.claims for e in c.evidence]


async def _turn(
    run: RunCtx, target: Target, views: dict[str, BaseModel], ids: frozenset[str],
    side: str, rnd: int, prior: list[DebateTurn], *, summary: bool, final: bool,
) -> AgentResult:  # fmt: skip
    body = {
        "task": "Argue your side from the analyst views only. Use no tools.",
        "as_of": run.as_of.isoformat(), "side": side, "round": rnd, "summary": summary,
        "final_round": final, "security_id": target.security_id, "symbol": target.symbol,
        "views": views_payload(views),
        "prior_turns": [t.model_dump(mode="json") for t in prior],
    }  # fmt: skip

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if not isinstance(model, DebateTurn):
            return ["unexpected output type"]
        errors = pointer_errors(_turn_refs(model), views)
        if model.security_id != target.security_id:
            errors.append(f"security_id must be {target.security_id}")
        if (model.side, model.round, model.summary) != (side, rnd, summary):
            errors.append(f"expected side {side}, round {rnd}, summary {summary}")
        if final and not summary and not model.strongest_unrebutted:
            errors.append("the final round must state strongest_unrebutted")
        return errors

    return await run.run(
        SPECS[side], json.dumps(body, sort_keys=True), security_id=target.security_id,
        extra_check=check, evidence_ids=ids,
    )  # fmt: skip


def _collect(debate: Debate, res: AgentResult, label: str) -> None:
    debate.results.append(res)
    if isinstance(res.output, DebateTurn):
        debate.turns.append(res.output)
    else:
        debate.failures.append(f"{label}: {res.reason}")


async def run_debate(run: RunCtx, target: Target, views: dict[str, BaseModel]) -> Debate:
    """Quick mode: one bull and one bear summary. Deep mode: N alternating rounds."""
    ids = analyst_tool_ids(run, target, views)
    debate = Debate()
    if run.mode == "quick":
        pair = await asyncio.gather(
            _turn(run, target, views, ids, "bull", 1, [], summary=True, final=False),
            _turn(run, target, views, ids, "bear", 1, [], summary=True, final=False),
        )
        for side, res in zip(("bull", "bear"), pair, strict=True):
            _collect(debate, res, f"{side} summary")
        return debate
    rounds = run.cfg.debate_rounds
    for rnd in range(1, rounds + 1):
        for side in ("bull", "bear"):
            res = await _turn(
                run, target, views, ids, side, rnd, list(debate.turns),
                summary=False, final=rnd == rounds,
            )  # fmt: skip
            _collect(debate, res, f"{side} round {rnd}")
    return debate
