"""Headless entry point: runs a `.claude/commands/<name>.md` prompt through the Agent SDK with a
deny-by-default tool surface. Claude Code interactive use reads the same `.claude/` files.
"""

import dataclasses
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pydantic import BaseModel

from nivesh_agents import validate
from nivesh_agents.prompts import Prompt, load_prompt
from nivesh_agents.schemas import MODELS
from nivesh_agents.specs import AgentSpec
from nivesh_agents.untrusted import GUARD
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.config import Price
from nivesh_core.cost import run_cost_inr
from nivesh_core.errors import NiveshError
from nivesh_core.redact import redact_text
from nivesh_core.trace import Tracer, prompt_version
from nivesh_mcp.registry import SERVERS

ROOT = Path(__file__).resolve().parents[1]
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Built-ins are already off (tools=[]); this is a second wall if that ever changes.
DISALLOWED = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"]


class AgentRunError(NiveshError):
    pass


def safe_error(e: BaseException) -> str:
    return redact_text(f"{type(e).__name__}: {e}")


def server_config(name: str) -> Any:
    """Stdio launch config of one of our read-only MCP servers."""
    return {"type": "stdio", "command": sys.executable, "args": ["-m", "nivesh_mcp", name]}


def build_options(
    *, mode: str, refresh: bool = False, env: dict[str, str] | None = None
) -> ClaudeAgentOptions:
    """prod and dev expose the same allow-listed MCP tools only; dev just gets more turns."""
    if mode not in ("dev", "prod"):
        raise ValueError(f"mode must be 'dev' or 'prod', got {mode!r}")
    run_env = dict(env or {})
    if refresh:
        run_env["NIVESH_REFRESH"] = "1"
    return ClaudeAgentOptions(
        tools=[],
        system_prompt=GUARD,
        allowed_tools=[f"mcp__{n}__{t}" for n, s in SERVERS.items() for t in s.tool_names],
        disallowed_tools=list(DISALLOWED),
        permission_mode="dontAsk",
        mcp_servers={n: server_config(n) for n in SERVERS},
        strict_mcp_config=True,
        setting_sources=["project"],
        cwd=ROOT,
        env=run_env,
        max_turns=30 if mode == "dev" else 15,
    )


def command_text(name: str) -> str:
    """Raw `.claude/commands/<name>.md` text (also the input to the trace's prompt_version)."""
    if not _NAME_RE.match(name):
        raise AgentRunError(f"invalid command name {name!r}")
    path = ROOT / ".claude" / "commands" / f"{name}.md"
    if not path.is_file():
        raise AgentRunError(f"unknown command {name!r}: no .claude/commands/{name}.md")
    return path.read_text()


def load_command(name: str, args: list[str]) -> str:
    text = command_text(name)
    if text.startswith("---\n"):
        text = text.split("---\n", 2)[2]
    return text.replace("$ARGUMENTS", " ".join(args)).strip()


def _trace_message(message: Any, tracer: Tracer) -> str | None:
    """Record one SDK message; returns the model name when the message carries it."""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                tracer.assistant_text(block.text)
            elif isinstance(block, ToolUseBlock):
                tracer.tool_call(block.id, block.name, block.input)
        return message.model
    if isinstance(message, UserMessage) and isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                tracer.tool_result(block.tool_use_id, block.content, block.is_error or False)
    return None


async def run_command(
    name: str,
    args: list[str],
    *,
    mode: str,
    refresh: bool = False,
    env: dict[str, str] | None = None,
    tracer: Tracer | None = None,
    prices: dict[str, Price] | None = None,
    usd_inr: float = 90.0,
) -> str:
    prompt = load_command(name, args)
    options = build_options(mode=mode, refresh=refresh, env=env)
    if tracer:
        tracer.start(name, prompt_version(command_text(name)))
    final: ResultMessage | None = None
    model: str | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if tracer:
                model = _trace_message(message, tracer) or model
            if isinstance(message, ResultMessage):
                final = message
        if final is None:
            raise AgentRunError("agent produced no result")
        if final.is_error:
            raise AgentRunError(
                redact_text(final.result or "; ".join(final.errors or []) or final.subtype)
            )
    except Exception as e:
        if tracer:
            tracer.error(safe_error(e))
        raise
    finally:  # failed runs still cost tokens; record whatever usage was seen
        if tracer and final is not None:
            usage = final.usage or {}
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            model = model or next(iter(final.model_usage or {}), None)
            cost, src = run_cost_inr(
                model, in_tok, out_tok, prices or {}, usd_inr, final.total_cost_usd
            )
            tracer.result(
                model=model, input_tokens=in_tok, output_tokens=out_tok,
                cost_inr=cost, cost_source=src,
            )  # fmt: skip
    return final.result or ""


# --- committee agents (ST-7.x): one query() per agent, validated, one repair ---


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_inr: float = 0.0
    cost_source: str = "none"


@dataclass(frozen=True)
class ToolCtx:
    """What the agent's own tool calls returned, kept in memory (the trace file is redacted)."""

    tool_names: dict[str, str]
    tool_results: dict[str, Any]


ExtraCheck = Callable[[BaseModel, ToolCtx], list[str]]


@dataclass(frozen=True)
class AgentResult:
    agent: str
    status: Literal["ok", "repaired", "failed"]
    model: str | None
    reason: str | None
    output: BaseModel | None
    ctx: ToolCtx
    usage: Usage
    prompt_version: int
    digest: str


@dataclass
class _Attempt:
    final: ResultMessage | None = None
    texts: list[str] = field(default_factory=list)
    model: str | None = None


def options_for(
    spec: AgentSpec, cfg: AgentsSettings, prompt: Prompt, *, env: dict[str, str] | None = None
) -> ClaudeAgentOptions:
    """Deny-by-default options for one agent: no built-ins, only its own allow-listed tools."""
    turns = cfg.max_turns.get(spec.name) or cfg.max_turns.get(spec.role) or spec.max_turns
    budget = cfg.max_budget_usd
    return ClaudeAgentOptions(
        tools=[],
        system_prompt=GUARD + "\n\n" + prompt.text,
        allowed_tools=list(spec.tools),
        disallowed_tools=list(DISALLOWED),
        permission_mode="dontAsk",
        mcp_servers={n: server_config(n) for n in spec.servers},
        strict_mcp_config=True,
        setting_sources=[],
        cwd=ROOT,
        env=dict(env or {}),
        max_turns=turns,
        model=getattr(cfg.models, cfg.tiers[spec.role]),
        max_budget_usd=float(budget) if budget is not None else None,
    )


async def _call(
    options: ClaudeAgentOptions, prompt: str, spec: AgentSpec, security_id: int | None,
    tracer: Tracer, ctx: ToolCtx,
) -> _Attempt:  # fmt: skip
    got = _Attempt()
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            got.model = message.model or got.model
            for block in message.content:
                if isinstance(block, TextBlock):
                    got.texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    ctx.tool_names[block.id] = block.name
                    tracer.tool_call(
                        block.id, block.name, block.input, agent=spec.name, security_id=security_id
                    )
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    ctx.tool_results[block.tool_use_id] = block.content
                    tracer.tool_result(
                        block.tool_use_id, block.content, block.is_error or False,
                        agent=spec.name, security_id=security_id,
                    )  # fmt: skip
        elif isinstance(message, ResultMessage):
            got.final = message
    return got


def _add_usage(
    usage: Usage, got: _Attempt, model: str | None, prices: dict[str, Price], usd_inr: float
) -> None:
    if got.final is None:
        return
    u = got.final.usage or {}
    in_tok, out_tok = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    cost, src = run_cost_inr(
        got.model or model, in_tok, out_tok, prices, usd_inr, got.final.total_cost_usd
    )
    usage.input_tokens += in_tok
    usage.output_tokens += out_tok
    usage.cost_inr += cost
    usage.cost_source = src if usage.cost_source in ("none", src) else "mixed"


def _judge(
    got: _Attempt, spec: AgentSpec, ids: frozenset[str], ctx: ToolCtx,
    extra_check: ExtraCheck | None,
) -> tuple[validate.Valid | validate.Invalid, str | None]:  # fmt: skip
    final = got.final
    if final is None:
        return validate.Invalid(["no result message"]), None
    text = final.result or (got.texts[-1] if got.texts else None)
    raw = validate.extract_json(final.structured_output, text)
    parsed = validate.parse(MODELS[spec.schema], raw)
    if isinstance(parsed, validate.Valid):
        errors = validate.check_evidence(parsed.model, ids)
        if extra_check is not None:
            errors += extra_check(parsed.model, ctx)
        if errors:
            return validate.Invalid(validate.clean_errors(errors)), raw
    return parsed, raw


async def run_agent(
    spec: AgentSpec,
    user_prompt: str,
    *,
    cfg: AgentsSettings,
    tracer: Tracer,
    security_id: int | None,
    extra_check: ExtraCheck | None = None,
    evidence_ids: frozenset[str] | None = None,
    tool_cap: tuple[str, int, str] | None = None,
    prices: dict[str, Price] | None = None,
    usd_inr: float = 90.0,
    env: dict[str, str] | None = None,
) -> AgentResult:
    """Run one agent. Invalid output gets exactly one repair call (same system prompt, no tools);
    a second failure, a raised error or an error result marks the agent failed and returns, so
    the caller's run continues. `tool_cap` is (tool name, most calls, failure reason): exceeding
    it fails the agent without spending the repair."""
    prompt = load_prompt(spec.name, pins=cfg.prompt_pins)
    options = options_for(spec, cfg, prompt, env=env)
    ctx, usage = ToolCtx({}, {}), Usage()
    tracer.agent_start(
        spec.name, security_id, model=options.model, prompt_version=f"v{prompt.version}",
        digest=prompt.digest,
    )  # fmt: skip
    status: Literal["ok", "repaired", "failed"] = "failed"
    reason: str | None = None
    output: BaseModel | None = None
    model = options.model
    try:
        got = await _call(options, user_prompt, spec, security_id, tracer, ctx)
        model = got.model or model
        _add_usage(usage, got, options.model, prices or {}, usd_inr)
        ids = evidence_ids if evidence_ids is not None else tracer.tool_ids(spec.name, security_id)
        if got.final is not None and got.final.is_error:
            raise AgentRunError(got.final.result or "; ".join(got.final.errors or []) or "error")
        if tool_cap and sum(1 for n in ctx.tool_names.values() if n == tool_cap[0]) > tool_cap[1]:
            reason = tool_cap[2]
        else:
            judged, raw = _judge(got, spec, ids, ctx, extra_check)
            if isinstance(judged, validate.Valid):
                status, output = "ok", judged.model
            else:
                again = dataclasses.replace(
                    options, allowed_tools=[], mcp_servers={}, max_turns=1,
                    resume=got.final.session_id if got.final and got.final.session_id else None,
                )  # fmt: skip
                text = validate.repair_prompt(
                    judged.errors, ids, None if again.resume else (raw or "")
                )
                got2 = await _call(again, text, spec, security_id, tracer, ctx)
                _add_usage(usage, got2, options.model, prices or {}, usd_inr)
                if got2.final is not None and got2.final.is_error:
                    raise AgentRunError(got2.final.result or "repair call failed")
                judged2, _ = _judge(got2, spec, ids, ctx, extra_check)
                if isinstance(judged2, validate.Valid):
                    status, output = "repaired", judged2.model
                else:
                    reason = "invalid output after repair: " + "; ".join(judged2.errors)[:500]
    except Exception as e:
        reason = redact_text(safe_error(e))[:500]
    if status == "failed":
        tracer.error(reason or "failed", agent=spec.name, security_id=security_id)
    tracer.agent_end(
        spec.name, security_id, status=status, model=model, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, cost_inr=usage.cost_inr, cost_source=usage.cost_source,
    )  # fmt: skip
    return AgentResult(
        spec.name, status, model, reason, output, ctx, usage, prompt.version, prompt.digest
    )
