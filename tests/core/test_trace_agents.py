import asyncio
import stat
from pathlib import Path

from nivesh_core.trace import Tracer, digest_text, prompt_version, read_trace


def make(tmp_path: Path) -> Tracer:
    ticks = iter([0.0, 0.25])
    return Tracer(tmp_path, 1, clock=lambda: next(ticks))


def recs(tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    return read_trace(tmp_path / "trace.jsonl")


def test_existing_single_run_trace_is_unchanged(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.start("ping", prompt_version("text"), "m")
    t.tool_call("tu1", "mcp__demo__ping", {"x": 1})
    t.tool_result("tu1", "pong")
    t.result(model="m", input_tokens=10, output_tokens=5, cost_inr=1.5, cost_source="table")
    r = recs(tmp_path)
    assert [x["type"] for x in r] == ["start", "tool_call", "tool_result", "result"]
    assert "agent" not in r[1] and "security_id" not in r[1]
    assert t.summary == {
        "model": "m", "input_tokens": 10, "output_tokens": 5, "latency_ms": 250,
        "cost_inr": 1.5, "cost_source": "table",
    }  # fmt: skip


def test_tool_call_carries_agent_and_security_attribution_in_meta(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.tool_call("tu1", "mcp__engine__ta_compute", {"a": 1}, agent="technical", security_id=7)
    t.tool_result("tu1", "ok", False, agent="technical", security_id=7)
    r = recs(tmp_path)
    assert (r[0]["agent"], r[0]["security_id"]) == ("technical", 7)
    assert (r[1]["agent"], r[1]["security_id"]) == ("technical", 7)


def test_tool_ids_index_is_per_agent_and_security(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.tool_call("a1", "x", {}, agent="news", security_id=1)
    t.tool_call("a2", "x", {}, agent="news", security_id=2)
    t.tool_call("b1", "x", {}, agent="technical", security_id=1)
    t.tool_call("m1", "x", {}, agent="macro", security_id=None)
    assert t.tool_ids("news", 1) == frozenset({"a1"})
    assert t.tool_ids("technical", 1) == frozenset({"b1"})
    assert t.tool_ids("macro", None) == frozenset({"m1"})
    assert t.tool_ids("pm", 1) == frozenset()
    assert t.all_tool_ids() == frozenset({"a1", "a2", "b1", "m1"})


def test_tool_ids_index_agrees_with_the_jsonl_file(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.tool_call("a1", "x", {}, agent="news", security_id=1)
    t.tool_call("b1", "x", {}, agent="technical", security_id=1)
    from_file = {x["tool_id"] for x in recs(tmp_path) if x["type"] == "tool_call"}
    assert from_file == set(t.all_tool_ids())


def test_agent_start_and_end_records_with_digest_and_status(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.agent_start("news", 3, model="sonnet", prompt_version="v1", digest="d" * 12)
    t.agent_end("news", 3, status="ok", model="sonnet", input_tokens=10, output_tokens=2,
                cost_inr=1.0, cost_source="table")  # fmt: skip
    r = recs(tmp_path)
    assert r[0]["type"] == "agent_start" and r[0]["prompt_digest"] == digest_text("d" * 12)
    assert r[0]["agent"] == "news" and r[0]["security_id"] == 3 and r[0]["model"] == "sonnet"
    assert r[1]["type"] == "agent_end" and r[1]["status"] == "ok" and r[1]["input_tokens"] == 10


def test_usage_accumulates_across_agents_instead_of_replacing(tmp_path: Path) -> None:
    t = make(tmp_path)
    for i, agent in enumerate(("news", "technical")):
        t.agent_end(agent, 1, status="ok", model="m" + str(i), input_tokens=10, output_tokens=3,
                    cost_inr=1.5, cost_source="table")  # fmt: skip
    assert t.totals == {"input_tokens": 20, "output_tokens": 6, "cost_inr": 3.0}


def test_finish_run_summary_has_the_same_keys_as_result_and_feeds_finish_run(
    tmp_path: Path,
) -> None:
    a = make(tmp_path)
    a.result(model="m", input_tokens=1, output_tokens=1, cost_inr=1.0, cost_source="table")
    b = Tracer(tmp_path / "x", 2, clock=iter([0.0, 1.0]).__next__)
    (tmp_path / "x").mkdir()
    b.agent_end("news", 1, status="ok", model="m", input_tokens=4, output_tokens=2,
                cost_inr=2.0, cost_source="sdk")  # fmt: skip
    b.finish_run("committee")
    assert set(b.summary) == set(a.summary)
    assert b.summary["model"] == "committee" and b.summary["input_tokens"] == 4
    assert b.summary["cost_source"] == "sdk" and b.summary["latency_ms"] == 1000
    assert read_trace(tmp_path / "x" / "trace.jsonl")[-1]["type"] == "result"


def test_mixed_cost_sources_summarise_as_mixed(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.agent_end("a", 1, status="ok", model="m", input_tokens=1, output_tokens=1, cost_inr=1.0,
                cost_source="sdk")  # fmt: skip
    t.agent_end("b", 1, status="ok", model="m", input_tokens=1, output_tokens=1, cost_inr=1.0,
                cost_source="table")  # fmt: skip
    t.finish_run("m")
    assert t.summary["cost_source"] == "mixed"


async def test_concurrent_agents_on_one_loop_write_unique_increasing_seq_and_valid_json_lines(
    tmp_path: Path,
) -> None:
    t = make(tmp_path)

    async def emit(n: int) -> None:
        for i in range(5):
            await asyncio.sleep(0)
            t.tool_call(f"id{n}{i}", "x", {"n": n}, agent=f"a{n}", security_id=n)

    await asyncio.gather(*(emit(n) for n in range(4)))
    r = recs(tmp_path)
    assert [x["seq"] for x in r] == list(range(1, 21))
    assert len(t.all_tool_ids()) == 20


def test_agent_output_payloads_are_not_stored_in_the_trace_only_digests(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.saved("output", "001_news_1.json", "ab" * 32)
    t.saved("snapshot", "snapshot.json", "cd" * 32)
    r = recs(tmp_path)
    assert [x["type"] for x in r] == ["output_saved", "snapshot_saved"]
    assert r[0]["digest"] == digest_text("ab" * 32) and r[0]["name"] == "001_news_1.json"
    assert set(r[0]) == {"seq", "ts", "type", "name", "digest"}


def test_error_can_carry_attribution(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.error("boom", agent="news", security_id=2)
    t.error("plain")
    r = recs(tmp_path)
    assert (r[0]["agent"], r[0]["security_id"]) == ("news", 2) and "agent" not in r[1]


def test_trace_is_private_0600(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.agent_start("news", 1, model="m", prompt_version="v1", digest="x")
    assert stat.S_IMODE((tmp_path / "trace.jsonl").stat().st_mode) == 0o600


def test_digest_text_has_no_run_of_nine_digits_even_for_an_all_digit_hash() -> None:
    import re

    out = digest_text("1234567890" * 6 + "1234")
    assert not re.search(r"\d{9}", out) and out.replace("-", "") == "1234567890" * 6 + "1234"
