import pytest

from nivesh_agents.prompts import load_prompt
from nivesh_agents.specs import SPECS
from nivesh_core.agents_config import AGENTS, AgentsSettings
from nivesh_mcp.base import is_write_name
from nivesh_mcp.registry import SERVERS

ENGINE_TOOLS = {
    "ta_compute", "fa_compute", "valuation_range", "red_flags", "risk_metrics", "portfolio_xray",
    "score", "mf_analyse", "mf_overlap", "get_fund_meta",
}  # fmt: skip


def m(server: str, *names: str) -> set[str]:
    return {f"mcp__{server}__{n}" for n in names}


D13 = {
    "fundamental": m("engine", "fa_compute", "valuation_range", "red_flags")
    | m("filings", "list_filings", "get_filing_text"),
    "technical": m("engine", "ta_compute"),
    "news": m("news", "get_news", "get_events_calendar", "get_next_results_date")
    | m("filings", "get_announcements"),
    "macro": m("macro", "get_series", "get_flows_india", "get_rates_snapshot")
    | m("market", "get_index"),
    "mf": m("engine", "mf_analyse", "mf_overlap", "get_fund_meta"),
    "risk": m("engine", "risk_metrics", "portfolio_xray", "red_flags"),
    "bull": set(), "bear": set(), "pm": set(),
    "lens_value": set(), "lens_growth": set(), "lens_contrarian": set(), "lens_valuation": set(),
}  # fmt: skip


def registered() -> set[str]:
    return {f"mcp__{n}__{t}" for n, s in SERVERS.items() for t in s.tool_names}


def test_every_spec_tool_is_a_registry_tool_and_none_is_write_named() -> None:
    known = registered()
    for name, spec in SPECS.items():
        for t in spec.tools:
            assert not is_write_name(t.split("__")[-1]), (name, t)
            if t.startswith("mcp__engine__") and "engine" not in SERVERS:
                pytest.skip("engine server is registered in step P5")
            assert t in known, (name, t)


def test_every_spec_allow_list_equals_the_documented_one_d_13() -> None:
    assert set(SPECS) == set(D13)
    for name, spec in SPECS.items():
        assert set(spec.tools) == D13[name], name
        assert len(spec.tools) == len(set(spec.tools))
    if "engine" not in SERVERS:
        pytest.skip("engine server is registered in step P5")
    assert {t.split("__")[2] for s in SPECS.values() for t in s.tools if "__engine__" in t} <= (
        ENGINE_TOOLS
    )


def test_debate_lens_and_pm_specs_have_empty_tools_and_no_servers() -> None:
    for name in (
        "bull",
        "bear",
        "pm",
        "lens_value",
        "lens_growth",
        "lens_contrarian",
        "lens_valuation",
    ):
        assert SPECS[name].tools == () and SPECS[name].servers == ()


def test_holdings_derived_engine_tools_are_only_on_the_risk_spec() -> None:
    holdings_derived = {"mcp__engine__risk_metrics", "mcp__engine__portfolio_xray"}
    owners = [n for n, s in SPECS.items() if holdings_derived & set(s.tools)]
    assert owners == ["risk"]
    for name, s in SPECS.items():
        assert "mcp__holdings" not in " ".join(s.tools), name


def test_spec_tiers_equal_the_pid_roster() -> None:
    cfg = AgentsSettings()
    assert {s.role for s in SPECS.values()} == set(AGENTS)
    tiers = {n: cfg.tiers[s.role] for n, s in SPECS.items()}
    assert tiers["fundamental"] == "top" and tiers["risk"] == "top" and tiers["pm"] == "top"
    assert tiers["bull"] == tiers["bear"] == "top"
    assert {tiers[n] for n in ("technical", "news", "macro", "mf", "lens_value")} == {"mid"}


def test_every_spec_has_a_prompt_whose_front_matter_agrees() -> None:
    from nivesh_agents.schemas import MODELS

    for name, spec in SPECS.items():
        p = load_prompt(name)
        assert p.agent == name and p.schema == spec.schema and spec.schema in MODELS
        assert set(p.tools) == set(spec.tools)
