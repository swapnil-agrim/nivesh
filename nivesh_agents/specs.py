"""Agent registry (PID 14.1). Each spec names its schema, its exact read-only tool allow-list
(plan D-13) and a default turn cap. Agents without tools (debate, lenses, portfolio manager)
get no servers at all; holdings-derived engine tools belong to the risk manager only.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    name: str  # prompt directory and trace name
    role: str  # key into `agents.tiers` and `agents.max_turns`
    schema: str  # key into `schemas.MODELS`
    tools: tuple[str, ...]
    max_turns: int

    @property
    def servers(self) -> tuple[str, ...]:
        return tuple(sorted({t.split("__")[1] for t in self.tools}))


def _mcp(server: str, *names: str) -> tuple[str, ...]:
    return tuple(f"mcp__{server}__{n}" for n in names)


_NEWS = _mcp("news", "get_news", "get_events_calendar", "get_next_results_date")
_NEWS += _mcp("filings", "get_announcements")
_MACRO = _mcp("macro", "get_series", "get_flows_india", "get_rates_snapshot")
_MACRO += _mcp("market", "get_index")
_FA = _mcp("engine", "fa_compute", "valuation_range", "red_flags")
_FA += _mcp("filings", "list_filings", "get_filing_text")

SPECS: dict[str, AgentSpec] = {
    s.name: s
    for s in (
        AgentSpec("fundamental", "fundamental", "AnalystView", _FA, 12),
        AgentSpec("technical", "technical", "AnalystView", _mcp("engine", "ta_compute"), 6),
        AgentSpec("news", "news", "AnalystView", _NEWS, 8),
        AgentSpec("macro", "macro", "MacroView", _MACRO, 8),
        AgentSpec(
            "mf", "mf", "FundView", _mcp("engine", "mf_analyse", "mf_overlap", "get_fund_meta"), 8
        ),
        AgentSpec("bull", "bull", "DebateTurn", (), 1),
        AgentSpec("bear", "bear", "DebateTurn", (), 1),
        AgentSpec("lens_value", "lens", "LensView", (), 1),
        AgentSpec("lens_growth", "lens", "LensView", (), 1),
        AgentSpec("lens_contrarian", "lens", "LensView", (), 1),
        AgentSpec("lens_valuation", "lens", "LensView", (), 1),
        AgentSpec(
            "risk",
            "risk",
            "RiskAssessment",
            _mcp("engine", "risk_metrics", "portfolio_xray", "red_flags"),
            8,
        ),
        AgentSpec("pm", "pm", "Verdict", (), 1),
    )
}
