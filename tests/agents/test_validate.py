import json
from pathlib import Path
from typing import Any

import pytest

from nivesh_agents import runtime, validate
from nivesh_agents.runtime import ToolCtx, run_agent
from nivesh_agents.schemas import MODELS
from nivesh_agents.specs import SPECS
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.trace import Tracer, read_trace
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import reply, script

CFG = AgentsSettings()
TECH = SPECS["technical"]
NEWS = SPECS["news"]
TA = ("mcp__news__get_news", {"q": "x"}, "headline text")


def tool(n: int = 1) -> tuple[str, str, dict[str, Any], Any]:
    return (fx.tid(n), TA[0], TA[1], TA[2])


def good_news(**over: Any) -> dict[str, Any]:
    return fx.analyst("news", **{"key_points": [fx.point(1)], **over})


def bad_news() -> dict[str, Any]:
    return fx.analyst("news", key_points=[fx.point(1)], score=500)


async def go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: Any, spec: Any = NEWS, **kw: Any
) -> tuple[Any, Tracer]:
    monkeypatch.setattr(runtime, "query", fake)
    tr = Tracer(tmp_path, 1)
    res = await run_agent(spec, "{}", cfg=CFG, tracer=tr, security_id=1, **kw)
    return res, tr


def test_structured_output_field_is_preferred_over_result_text() -> None:
    assert json.loads(validate.extract_json({"a": 1}, '{"b": 2}') or "") == {"a": 1}
    assert json.loads(validate.extract_json(None, '{"b": 2}') or "") == {"b": 2}


def test_last_json_object_is_extracted_from_text_with_trailing_prose() -> None:
    text = 'first {"a": 1} then {"b": {"c": 2}} and trailing words {not json'
    assert json.loads(validate.extract_json(None, text) or "") == {"b": {"c": 2}}


def test_non_json_output_is_invalid_with_a_clear_error() -> None:
    assert validate.extract_json(None, "no objects here") is None
    out = validate.parse(MODELS["AnalystView"], None)
    assert isinstance(out, validate.Invalid) and "JSON object" in out.errors[0]


async def test_valid_first_time_makes_one_query_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply(good_news(), tools=(tool(1),))])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "ok" and fake.counts == {"news": 1} and res.output is not None


async def test_invalid_then_valid_makes_exactly_two_query_calls_and_status_ok_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply(bad_news(), tools=(tool(1),)), reply(good_news())])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "repaired" and fake.counts == {"news": 2} and res.reason is None


async def test_invalid_twice_makes_exactly_two_query_calls_and_status_failed_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply(bad_news()), reply("not json at all")])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "failed" and fake.counts == {"news": 2} and res.output is None
    assert res.reason and "after repair" in res.reason


async def test_third_attempt_is_never_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = script(news=[reply(bad_news()), reply(bad_news()), reply(good_news())])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "failed" and len(fake.seen) == 2 and len(fake.by_agent["news"]) == 1  # type: ignore[arg-type]
    assert validate.REPAIR_RETRIES == 1


async def test_repair_prompt_lists_the_validation_errors_and_the_valid_tool_call_ids_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = reply(bad_news(), tools=(tool(2), tool(1)))
    fake = script(news=[first, reply(good_news())])
    await go(tmp_path, monkeypatch, fake)
    text = fake.seen[1].prompt
    assert "score" in text and "rejected" in text
    assert text.index(fx.tid(1)) < text.index(fx.tid(2)) and "JSON only" in text


async def test_repair_call_has_no_tools_no_servers_max_turns_1_and_resumes_the_session_when_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply(bad_news(), tools=(tool(1),), session="sess-9"), reply(good_news())])
    await go(tmp_path, monkeypatch, fake)
    first, second = fake.seen[0].options, fake.seen[1].options
    assert first.allowed_tools == list(NEWS.tools) and first.resume is None
    assert second.allowed_tools == [] and second.mcp_servers == {} and second.max_turns == 1
    assert second.resume == "sess-9" and second.tools == []
    assert second.system_prompt == first.system_prompt  # same GUARD + prompt (refinement 7)


async def test_repair_without_session_id_carries_the_first_output_in_a_fresh_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply(bad_news(), tools=(tool(1),), session=None), reply(good_news())])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "repaired" and fake.seen[1].options.resume is None
    assert '"score": 500' in fake.seen[1].prompt


async def test_evidence_id_missing_from_the_agents_own_tool_ids_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = good_news(key_points=[fx.point(99)])
    fake = script(news=[reply(ghost, tools=(tool(1),)), reply(ghost)])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "failed" and "not a tool call" in (res.reason or "")


async def test_evidence_id_from_another_agents_calls_is_invalid_for_an_analyst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", script(news=[reply(good_news()), reply(good_news())]))
    tr = Tracer(tmp_path, 1)
    tr.tool_call(fx.tid(1), "mcp__engine__ta_compute", {}, agent="technical", security_id=1)
    res = await run_agent(NEWS, "{}", cfg=CFG, tracer=tr, security_id=1)
    assert res.status == "failed"  # tid(1) belongs to the technical agent, not to news


async def test_evidence_ids_may_come_from_the_analyst_union_for_debate_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = fx.turn(claims=[{"claim": "c", "evidence": [{"tool_call_id": fx.tid(7)}]}])
    fake = script(bull=[reply(turn)])
    res, _ = await go(
        tmp_path, monkeypatch, fake, SPECS["bull"], evidence_ids=frozenset({fx.tid(7)})
    )
    assert res.status == "ok"


def test_error_text_is_redacted_and_truncated() -> None:
    out = validate.clean_errors(["bad " + "Bea" + "rer abc.def123", "x" * 5000, "never seen"])
    assert "abc.def123" not in out[0]
    assert sum(len(e) for e in out) <= validate.ERROR_LIMIT and len(out) == 2


async def test_exception_from_query_marks_failed_without_a_repair_and_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[RuntimeError("boom " + "Bea" + "rer tok.en123"), reply(good_news())])
    res, tr = await go(tmp_path, monkeypatch, fake)
    assert res.status == "failed" and fake.counts == {"news": 1}
    assert "boom" in (res.reason or "") and "tok.en123" not in (res.reason or "")


async def test_failed_agent_writes_an_error_trace_record_and_the_caller_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ta_view = fx.analyst("technical", levels=None)
    fake = script(
        news=[reply(bad_news()), reply(bad_news())],
        technical=[reply(ta_view, tools=(tool(5),))],
    )
    monkeypatch.setattr(runtime, "query", fake)
    tr = Tracer(tmp_path, 1)
    bad = await run_agent(NEWS, "{}", cfg=CFG, tracer=tr, security_id=1)
    nxt = await run_agent(TECH, "{}", cfg=CFG, tracer=tr, security_id=1)
    assert bad.status == "failed" and nxt.status in ("ok", "failed")
    recs = read_trace(tmp_path / "trace.jsonl")
    errs = [r for r in recs if r["type"] == "error"]
    assert errs and errs[0]["agent"] == "news"
    ends = [r for r in recs if r["type"] == "agent_end"]
    assert [e["agent"] for e in ends] == ["news", "technical"] and ends[0]["status"] == "failed"


async def test_error_result_fails_the_agent_without_a_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply("it broke", is_error=True), reply(good_news())])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert (
        res.status == "failed" and fake.counts == {"news": 1} and "it broke" in (res.reason or "")
    )


async def test_no_result_message_is_invalid_and_repairable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(news=[reply("x", tools=(tool(1),))[:2], reply(good_news())])
    res, _ = await go(tmp_path, monkeypatch, fake)
    assert res.status == "repaired"


async def test_tool_cap_fails_without_spending_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tuple((fx.tid(i), "mcp__news__get_news", {}, "r") for i in (1, 2, 3))
    fake = script(news=[reply(good_news(), tools=calls), reply(good_news())])
    res, _ = await go(
        tmp_path, monkeypatch, fake, tool_cap=("mcp__news__get_news", 2, "filing_section_cap")
    )
    assert (
        res.status == "failed" and res.reason == "filing_section_cap" and fake.counts == {"news": 1}
    )


async def test_extra_check_errors_trigger_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ToolCtx] = []

    def check(model: Any, ctx: ToolCtx) -> list[str]:
        seen.append(ctx)
        return ["levels differ"] if len(seen) == 1 else []

    fake = script(news=[reply(good_news(), tools=(tool(1),)), reply(good_news())])
    res, _ = await go(tmp_path, monkeypatch, fake, extra_check=check)
    assert res.status == "repaired" and seen[0].tool_results[fx.tid(1)] == "headline text"
    assert "levels differ" in fake.seen[1].prompt
