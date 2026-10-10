"""Investing-philosophy lenses (ST-7.7), deep mode only. Each lens reads the analyst views with
no tools and returns a take-the-position or pass opinion. Lenses inform the portfolio manager and
never override it; `disagreement_count` is computed from the final verdict after the fact and
feeds nothing back into it.
"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from nivesh_agents.analysts import Target
from nivesh_agents.context import RunCtx
from nivesh_agents.debate import analyst_tool_ids, pointer_errors, views_payload
from nivesh_agents.runtime import AgentResult, ToolCtx
from nivesh_agents.schemas import LensView
from nivesh_agents.specs import SPECS

UPWARD_VERDICTS = ("BUY", "ACCUMULATE")


@dataclass
class Lenses:
    views: list[LensView] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    results: list[AgentResult] = field(default_factory=list)


def lenses_enabled(run: RunCtx) -> bool:
    return run.mode == "deep" and run.cfg.lenses_enabled


async def run_lenses(run: RunCtx, target: Target, views: dict[str, BaseModel]) -> Lenses:
    out = Lenses()
    if not lenses_enabled(run):
        return out
    ids = analyst_tool_ids(run, target, views)

    async def one(name: str) -> AgentResult:
        body = {
            "task": "Give your lens opinion from the analyst views only. Use no tools.",
            "as_of": run.as_of.isoformat(), "lens": name, "security_id": target.security_id,
            "symbol": target.symbol, "views": views_payload(views),
        }  # fmt: skip

        def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
            if not isinstance(model, LensView):
                return ["unexpected output type"]
            errors = pointer_errors(list(model.evidence), views)
            if model.lens != name or model.security_id != target.security_id:
                errors.append(f"expected lens {name} for security {target.security_id}")
            return errors

        return await run.run(
            SPECS[f"lens_{name}"], json.dumps(body, sort_keys=True),
            security_id=target.security_id, extra_check=check, evidence_ids=ids,
        )  # fmt: skip

    results = await asyncio.gather(*(one(n) for n in run.cfg.lenses))
    for name, res in zip(run.cfg.lenses, results, strict=True):
        out.results.append(res)
        if isinstance(res.output, LensView):
            out.views.append(res.output)
        else:
            out.failures.append(f"lens {name}: {res.reason}")
    return out


def disagreement_count(lens_views: Sequence[LensView], final_verdict: str) -> int:
    """Lenses whose opinion (take the position or pass) differs from the final verdict being
    upward or not.
    A lens that failed has no view and is not counted."""
    upward = final_verdict in UPWARD_VERDICTS
    return sum((v.decision == "would_buy") != upward for v in lens_views)
