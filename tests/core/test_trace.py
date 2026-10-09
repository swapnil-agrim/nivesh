import stat
from pathlib import Path

from nivesh_core.pii_scan import scan_paths
from nivesh_core.trace import Tracer, prompt_version, read_trace
from tests import pii_values as pv


def make(tmp_path: Path) -> Tracer:
    ticks = iter([0.0, 0.25])
    return Tracer(tmp_path, 1, clock=lambda: next(ticks))


def test_records_in_order_and_private(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.start("ping", prompt_version("text"), "m")
    t.assistant_text("hello")
    t.tool_call("tu1", "mcp__demo__ping", {"x": 1})
    t.tool_result("tu1", "pong")
    t.engine_call("add", [1, 2], 3)
    t.result(model="m", input_tokens=10, output_tokens=5, cost_inr=1.5, cost_source="table")
    recs = read_trace(tmp_path / "trace.jsonl")
    assert [r["type"] for r in recs] == [
        "start", "assistant_text", "tool_call", "tool_result", "engine_call", "result",
    ]  # fmt: skip
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5, 6]
    assert (
        recs[0]["prompt_version"] == prompt_version("text") and len(recs[0]["prompt_version"]) == 12
    )
    assert recs[2]["tool"] == "mcp__demo__ping" and recs[2]["args"] == {"x": 1}
    last = recs[-1]
    assert (
        last["input_tokens"] == 10 and last["latency_ms"] == 250 and last["cost_source"] == "table"
    )
    assert stat.S_IMODE((tmp_path / "trace.jsonl").stat().st_mode) == 0o600


def test_payloads_are_redacted(tmp_path: Path) -> None:
    t = make(tmp_path)
    t.assistant_text(pv.pan() + " and " + pv.email())
    t.tool_call("t", "x", {"note": pv.phone()})
    t.tool_result("t", {"rows": [pv.folio()]})
    t.error(pv.bearer())
    assert scan_paths([tmp_path]) == []
    recs = read_trace(tmp_path / "trace.jsonl")
    assert pv.pan() not in recs[0]["text"] and recs[1]["tool"] == "x"
