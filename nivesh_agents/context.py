"""Per-run state shared by the committee stages: settings, tracer, the global concurrency slots,
and the once-per-run macro task. Nothing here is module-global, so two runs never share a view.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from nivesh_agents import runtime
from nivesh_agents.runtime import AgentResult, ExtraCheck
from nivesh_agents.specs import AgentSpec
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.config import Price
from nivesh_core.trace import Tracer

Mode = Literal["quick", "deep"]


@dataclass
class RunCtx:
    cfg: AgentsSettings
    tracer: Tracer
    as_of: date
    mode: Mode
    prices: dict[str, Price] = field(default_factory=dict)
    usd_inr: float = 90.0
    env: dict[str, str] = field(default_factory=dict)
    sem: asyncio.Semaphore | None = None  # one per run: total live agents <= concurrency
    macro_task: "asyncio.Task[AgentResult] | None" = None

    def slots(self) -> asyncio.Semaphore:
        if self.sem is None:
            self.sem = asyncio.Semaphore(self.cfg.concurrency)
        return self.sem

    async def run(
        self,
        spec: AgentSpec,
        user_prompt: str,
        *,
        security_id: int | None,
        extra_check: ExtraCheck | None = None,
        evidence_ids: frozenset[str] | None = None,
        tool_cap: tuple[str, int, str] | None = None,
    ) -> AgentResult:
        """One agent, holding one concurrency slot for the duration of its calls."""
        async with self.slots():
            return await runtime.run_agent(
                spec, user_prompt, cfg=self.cfg, tracer=self.tracer, security_id=security_id,
                extra_check=extra_check, evidence_ids=evidence_ids, tool_cap=tool_cap,
                prices=self.prices, usd_inr=self.usd_inr, env=self.env,
            )  # fmt: skip
