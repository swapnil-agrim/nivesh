"""The analyst agents (ST-7.2 to ST-7.5): input builders and the extra output checks.

Analysts see only their own tools and the security under study, never each other's output. The
technical analyst's numbers must equal the engine's (compared as Decimals); the fundamental
analyst may read at most `max_filing_sections` filing texts.
"""

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from nivesh_agents.context import RunCtx
from nivesh_agents.runtime import AgentResult, ToolCtx
from nivesh_agents.schemas import AnalystView, FundView, MacroView
from nivesh_agents.specs import SPECS

QUANTUM = Decimal("0.00000001")  # the engine's quantum for levels
TA_TOOL = "mcp__engine__ta_compute"
FILING_TEXT = "mcp__filings__get_filing_text"
LEVEL_FIELDS = ("entry_low", "entry_high", "stop", "invalidation")
NO_SETUP = (None, "none")


@dataclass(frozen=True)
class Target:
    """The security under study. Holds identifiers only: no quantities, values or accounts."""

    security_id: int
    symbol: str
    name: str
    kind: str = "equity"  # equity, etf or mf
    market: str = "IN"
    scheme: str | None = None  # AMFI code, for funds


def analyst_prompt(run: RunCtx, target: Target, task: str) -> str:
    body = {
        "task": task, "as_of": run.as_of.isoformat(), "mode": run.mode,
        "security": {
            "security_id": target.security_id, "symbol": target.symbol, "name": target.name,
            "kind": target.kind, "market": target.market,
            **({"scheme": target.scheme} if target.scheme else {}),
        },
    }  # fmt: skip
    return json.dumps(body, sort_keys=True)


def _identity(
    model: BaseModel, agent: str, security_id: int | None, run: RunCtx
) -> list[str]:  # fmt: skip
    errors = []
    if getattr(model, "agent", agent) != agent:
        errors.append(f"agent must be {agent!r}")
    if security_id is not None and getattr(model, "security_id", security_id) != security_id:
        errors.append(f"security_id must be {security_id}")
    if getattr(model, "as_of", run.as_of) != run.as_of:
        errors.append(f"as_of must be {run.as_of.isoformat()}")
    return errors


def tool_json(content: Any) -> Any:
    """The JSON object a tool result carries (a string or MCP text blocks), else None."""
    if isinstance(content, list):
        content = "".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str):
        return None
    try:
        return json.loads(content)
    except ValueError:
        return None


def _q(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(QUANTUM)
    except (ArithmeticError, ValueError):
        return None


def engine_setup(ctx: ToolCtx) -> dict[str, Any] | None:
    """The `setup` block of this agent's own ta_compute result, or None when it made no call."""
    for tool_id, name in ctx.tool_names.items():
        if name == TA_TOOL:
            payload = tool_json(ctx.tool_results.get(tool_id))
            try:
                setup = payload["data"]["result"]["setup"]
            except (TypeError, KeyError):
                continue
            if isinstance(setup, dict):
                return setup
    return None


def technical_numbers(view: AnalystView, ctx: ToolCtx) -> list[str]:
    """Every level and `setup.*` evidence value must equal the engine's own figure."""
    setup = engine_setup(ctx)
    given = view.levels
    quoted = [e for p in view.key_points for e in p.evidence if e.field.startswith("setup.")]
    if setup is None:
        return (
            ["levels or setup values given without a ta_compute result"]
            if (given or quoted)
            else []
        )
    errors: list[str] = []
    if setup.get("setup") in NO_SETUP:
        if given is not None and any(
            getattr(given, f) is not None for f in (*LEVEL_FIELDS, "setup_type")
        ):
            errors.append("the engine found no setup, so levels must be null")
    elif given is not None:
        if given.setup_type is not None and given.setup_type != setup.get("setup"):
            errors.append(f"setup_type differs from the engine value {setup.get('setup')!r}")
        for f in LEVEL_FIELDS:
            mine, theirs = getattr(given, f), _q(setup.get(f))
            if mine is not None and (theirs is None or mine.quantize(QUANTUM) != theirs):
                errors.append(f"levels.{f} differs from the engine value {setup.get(f)!r}")
    for e in quoted:
        key = e.field.removeprefix("setup.")
        if key not in setup:
            errors.append(f"evidence field {e.field!r} is not in the engine setup")
        elif isinstance(e.value, str):
            if e.value != setup[key]:
                errors.append(f"evidence {e.field} differs from the engine value {setup[key]!r}")
        elif (_q(e.value) != _q(setup[key])) or setup[key] is None:
            errors.append(f"evidence {e.field} differs from the engine value {setup[key]!r}")
    return errors


async def run_fundamental(run: RunCtx, target: Target) -> AgentResult:
    prompt = analyst_prompt(run, target, "Assess quality, growth, balance sheet and valuation.")

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        return _identity(model, "fundamental", target.security_id, run)

    cap = (FILING_TEXT, run.cfg.max_filing_sections, "filing_section_cap")
    return await run.run(
        SPECS["fundamental"], prompt, security_id=target.security_id, extra_check=check,
        tool_cap=cap,
    )  # fmt: skip


async def run_technical(run: RunCtx, target: Target) -> AgentResult:
    prompt = analyst_prompt(run, target, "Assess trend, momentum, levels and the positional setup.")

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if not isinstance(model, AnalystView):
            return ["unexpected output type"]
        errors = _identity(model, "technical", target.security_id, run)
        return errors + technical_numbers(model, ctx)

    return await run.run(
        SPECS["technical"], prompt, security_id=target.security_id, extra_check=check
    )


async def run_news(run: RunCtx, target: Target) -> AgentResult:
    prompt = analyst_prompt(run, target, "Find material events, tone shifts and catalysts.")

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        return _identity(model, "news", target.security_id, run)

    return await run.run(SPECS["news"], prompt, security_id=target.security_id, extra_check=check)


async def run_mf(run: RunCtx, target: Target) -> AgentResult:
    prompt = analyst_prompt(run, target, "Assess fund quality, cost, overlap and category fit.")

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if not isinstance(model, FundView):
            return ["unexpected output type"]
        errors = _identity(model, "mf", target.security_id, run)
        if model.scheme != target.scheme:
            errors.append(f"scheme must be {target.scheme!r}")
        return errors

    return await run.run(SPECS["mf"], prompt, security_id=target.security_id, extra_check=check)


async def _macro(run: RunCtx) -> AgentResult:
    body = json.dumps(
        {"task": "Assess rates, flows, currency and the market regime.",
         "as_of": run.as_of.isoformat()}, sort_keys=True,
    )  # fmt: skip

    def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
        if not isinstance(model, MacroView):
            return ["unexpected output type"]
        return _identity(model, "macro", None, run)

    return await run.run(SPECS["macro"], body, security_id=None, extra_check=check)


async def macro_once(run: RunCtx) -> AgentResult:
    """The market-wide view, computed once per run and shared by every security. A failure is
    shared too (not retried per security). Waiters hold no concurrency slot; only the single
    macro call does."""
    if run.macro_task is None:
        run.macro_task = asyncio.ensure_future(_macro(run))
    return await asyncio.shield(run.macro_task)


RUNNERS = {"fundamental": run_fundamental, "technical": run_technical, "news": run_news,
           "mf": run_mf}  # fmt: skip
QUICK_ANALYSTS = ("fundamental", "technical", "news")


async def run_analyst(run: RunCtx, name: str, target: Target) -> AgentResult:
    return await RUNNERS[name](run, target)
