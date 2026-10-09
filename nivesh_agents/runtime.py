"""Headless entry point: runs a `.claude/commands/<name>.md` prompt through the Agent SDK with a
deny-by-default tool surface. Claude Code interactive use reads the same `.claude/` files.
"""

import re
import sys
from pathlib import Path
from typing import Any

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

from nivesh_agents.untrusted import GUARD
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
        mcp_servers={
            n: {"type": "stdio", "command": sys.executable, "args": ["-m", "nivesh_mcp", n]}
            for n in SERVERS
        },
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
