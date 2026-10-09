"""Headless entry point: runs a `.claude/commands/<name>.md` prompt through the Agent SDK with a
deny-by-default tool surface. Claude Code interactive use reads the same `.claude/` files.
"""

import re
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from nivesh_core.errors import NiveshError
from nivesh_core.redact import redact_text
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


def load_command(name: str, args: list[str]) -> str:
    if not _NAME_RE.match(name):
        raise AgentRunError(f"invalid command name {name!r}")
    path = ROOT / ".claude" / "commands" / f"{name}.md"
    if not path.is_file():
        raise AgentRunError(f"unknown command {name!r}: no .claude/commands/{name}.md")
    text = path.read_text()
    if text.startswith("---\n"):
        text = text.split("---\n", 2)[2]
    return text.replace("$ARGUMENTS", " ".join(args)).strip()


async def run_command(
    name: str,
    args: list[str],
    *,
    mode: str,
    refresh: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    prompt = load_command(name, args)
    options = build_options(mode=mode, refresh=refresh, env=env)
    final: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            final = message
    if final is None:
        raise AgentRunError("agent produced no result")
    if final.is_error:
        raise AgentRunError(
            redact_text(final.result or "; ".join(final.errors or []) or final.subtype)
        )
    return final.result or ""
