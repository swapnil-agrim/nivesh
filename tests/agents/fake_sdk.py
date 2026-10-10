"""A scripted stand-in for the SDK `query` (no network, no model). Install with
`monkeypatch.setattr(runtime, "query", fake)` exactly as tests/agents/test_runtime.py does.

The calling agent is read from the `[agent:<name>]` marker in the system prompt. Per agent, give
either a list of attempts (popped in order: first call, then the repair call) or a callable
`(Call) -> attempt` used for every call. An attempt is a list of SDK messages or an Exception.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

MARKER = re.compile(r"\[agent:([a-z_]+)\]")
Attempt = list[Any] | Exception


@dataclass(frozen=True)
class Call:
    agent: str
    prompt: str
    options: ClaudeAgentOptions


def reply(
    payload: dict[str, Any] | str,
    *,
    tools: tuple[tuple[str, str, dict[str, Any], Any], ...] = (),
    session: str | None = "sess",
    structured: bool = False,
    model: str = "claude-test",
    usage: tuple[int, int] = (100, 20),
    cost_usd: float = 0.01,
    is_error: bool = False,
) -> list[Any]:
    """tools = ((tool_use_id, tool_name, input, result_content), ...)"""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    out: list[Any] = []
    if tools:
        out.append(
            AssistantMessage(content=[ToolUseBlock(i, n, a) for i, n, a, _ in tools], model=model)
        )
        out.append(
            UserMessage(content=[ToolResultBlock(tool_use_id=i, content=r) for i, _, _, r in tools])
        )
    out.append(AssistantMessage(content=[TextBlock(text=text)], model=model))
    out.append(
        ResultMessage(
            subtype="error" if is_error else "success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=is_error,
            num_turns=1,
            session_id=session or "",
            total_cost_usd=cost_usd,
            usage={"input_tokens": usage[0], "output_tokens": usage[1]},
            result=None if structured else text,
            structured_output=payload if structured and isinstance(payload, dict) else None,
        )  # fmt: skip
    )
    return out


class FakeSDK:
    def __init__(self, **by_agent: list[Attempt] | Callable[[Call], Attempt]) -> None:
        self.by_agent = {k: (list(v) if isinstance(v, list) else v) for k, v in by_agent.items()}
        self.seen: list[Call] = []
        self.counts: dict[str, int] = {}
        self.peak = 0
        self.open = 0
        self.delay = 0.0
        self.on_start: Callable[[Call], None] | None = None

    def calls(self, agent: str) -> list[Call]:
        return [c for c in self.seen if c.agent == agent]

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        m = MARKER.search(str(options.system_prompt))
        assert m, "system prompt has no [agent:<name>] marker"
        call = Call(m.group(1), prompt, options)
        self.seen.append(call)
        self.counts[call.agent] = self.counts.get(call.agent, 0) + 1
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            if self.on_start:
                self.on_start(call)
            if self.delay:
                await asyncio.sleep(self.delay)
            src = self.by_agent.get(call.agent)
            assert src is not None, f"unscripted agent {call.agent}"
            if callable(src):
                attempt = src(call)
            else:
                assert src, f"no scripted attempt left for {call.agent}"
                attempt = src.pop(0)
            if isinstance(attempt, Exception):
                raise attempt
            for message in attempt:
                yield message
        finally:
            self.open -= 1


def script(**by_agent: list[Attempt] | Callable[[Call], Attempt]) -> FakeSDK:
    return FakeSDK(**by_agent)
