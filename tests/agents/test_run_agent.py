import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nivesh_agents import runtime
from nivesh_agents.prompts import load_prompt
from nivesh_agents.runtime import DISALLOWED, build_options, options_for, run_agent
from nivesh_agents.specs import SPECS
from nivesh_agents.untrusted import GUARD
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.config import Price
from nivesh_core.trace import Tracer, digest_text, read_trace
from nivesh_mcp.registry import SERVERS
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import reply, script

CFG = AgentsSettings()


def news_view() -> dict[str, Any]:
    return fx.analyst("news", key_points=[fx.point(1)])


NEWS_TOOL = (fx.tid(1), "mcp__news__get_news", {"q": "x"}, "headline text")


def test_options_use_tools_empty_dontask_strict_mcp_empty_sources_and_disallowed() -> None:
    for name, spec in SPECS.items():
        o = options_for(spec, CFG, load_prompt(name))
        assert o.tools == [] and o.permission_mode == "dontAsk"
        assert o.strict_mcp_config is True and o.setting_sources == []
        assert o.disallowed_tools == DISALLOWED
        assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "Agent"} <= set(
            o.disallowed_tools
        )


def test_system_prompt_is_guard_plus_the_prompt_text_with_the_agent_marker() -> None:
    p = load_prompt("news")
    o = options_for(SPECS["news"], CFG, p)
    assert o.system_prompt == GUARD + "\n\n" + p.text
    assert "[agent:news]" in str(o.system_prompt)


def test_model_comes_from_the_tier_map_in_config() -> None:
    cfg = AgentsSettings(models={"top": "m-top", "mid": "m-mid", "small": "m-small"})  # type: ignore[arg-type]
    assert options_for(SPECS["fundamental"], cfg, load_prompt("fundamental")).model == "m-top"
    assert options_for(SPECS["technical"], cfg, load_prompt("technical")).model == "m-mid"
    assert options_for(SPECS["lens_value"], cfg, load_prompt("lens_value")).model == "m-mid"
    cfg2 = AgentsSettings(tiers={**CFG.tiers, "news": "small"})
    assert options_for(SPECS["news"], cfg2, load_prompt("news")).model == "haiku"


def test_only_the_specs_servers_are_started_and_only_its_tools_are_allowed() -> None:
    o = options_for(SPECS["news"], CFG, load_prompt("news"))
    assert set(o.mcp_servers) == {"news", "filings"}  # type: ignore[arg-type]
    assert o.allowed_tools == list(SPECS["news"].tools)
    assert o.mcp_servers["news"] == {  # type: ignore[index]
        "type": "stdio", "command": sys.executable, "args": ["-m", "nivesh_mcp", "news"],
    }  # fmt: skip
    pm = options_for(SPECS["pm"], CFG, load_prompt("pm"))
    assert pm.allowed_tools == [] and pm.mcp_servers == {}


def test_max_turns_and_budget_come_from_config() -> None:
    p = load_prompt("news")
    assert options_for(SPECS["news"], CFG, p).max_turns == SPECS["news"].max_turns
    assert options_for(SPECS["news"], CFG, p).max_budget_usd is None
    cfg = AgentsSettings(max_turns={"news": 3}, max_budget_usd=Decimal("0.5"))
    o = options_for(SPECS["news"], cfg, p)
    assert o.max_turns == 3 and o.max_budget_usd == 0.5
    cfg2 = AgentsSettings(max_turns={"lens": 2})
    assert options_for(SPECS["lens_growth"], cfg2, load_prompt("lens_growth")).max_turns == 2


async def test_tool_use_blocks_are_traced_with_agent_and_raw_results_kept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", script(news=[reply(news_view(), tools=(NEWS_TOOL,))]))
    tr = Tracer(tmp_path, 1)
    res = await run_agent(SPECS["news"], "{}", cfg=CFG, tracer=tr, security_id=4)
    assert res.status == "ok"
    assert res.ctx.tool_results == {fx.tid(1): "headline text"}
    assert res.ctx.tool_names == {fx.tid(1): "mcp__news__get_news"}
    recs = read_trace(tmp_path / "trace.jsonl")
    call = next(r for r in recs if r["type"] == "tool_call")
    assert (call["agent"], call["security_id"], call["tool_id"]) == ("news", 4, fx.tid(1))
    assert tr.tool_ids("news", 4) == frozenset({fx.tid(1)})


async def test_usage_is_accumulated_into_the_tracer_with_cost_from_the_price_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = fx.analyst("news", score=900)
    monkeypatch.setattr(
        runtime,
        "query",
        script(news=[
            reply(bad, tools=(NEWS_TOOL,), usage=(100, 10), model="sonnet"),
            reply(news_view(), usage=(50, 5), model="sonnet"),
        ]),
    )  # fmt: skip
    prices = {"sonnet": Price(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)}
    tr = Tracer(tmp_path, 1)
    res = await run_agent(
        SPECS["news"], "{}", cfg=CFG, tracer=tr, security_id=1, prices=prices, usd_inr=100.0
    )
    assert res.status == "repaired"
    assert (res.usage.input_tokens, res.usage.output_tokens) == (150, 15)
    assert res.usage.cost_source == "table" and res.usage.cost_inr > 0
    assert tr.totals["input_tokens"] == 150 and tr.totals["cost_inr"] == res.usage.cost_inr


async def test_agent_start_and_end_are_traced_with_prompt_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", script(news=[reply(news_view(), tools=(NEWS_TOOL,))]))
    tr = Tracer(tmp_path, 1)
    res = await run_agent(SPECS["news"], "{}", cfg=CFG, tracer=tr, security_id=2)
    recs = read_trace(tmp_path / "trace.jsonl")
    start, end = recs[0], recs[-1]
    assert start["type"] == "agent_start" and start["prompt_version"] == "v1"
    assert start["prompt_digest"] == digest_text(load_prompt("news").digest)
    assert res.digest == load_prompt("news").digest
    assert start["model"] == "sonnet"
    assert end["type"] == "agent_end" and end["status"] == "ok" and end["security_id"] == 2


def test_run_command_options_are_unchanged_by_the_refactor() -> None:
    o = build_options(mode="prod")
    assert o.mcp_servers == {
        n: {"type": "stdio", "command": sys.executable, "args": ["-m", "nivesh_mcp", n]}
        for n in SERVERS
    }
    assert o.setting_sources == ["project"] and o.system_prompt == GUARD and o.max_turns == 15
    assert runtime.server_config("x") == {
        "type": "stdio", "command": sys.executable, "args": ["-m", "nivesh_mcp", "x"],
    }  # fmt: skip
