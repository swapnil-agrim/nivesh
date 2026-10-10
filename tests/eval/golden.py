"""Golden-set scorer for the fundamental analyst (ST-7.2 AC2): the harness only. Run offline it
checks the scorer against a scripted fake; the live run (>= 8 of 10) is a separate, marked test.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from nivesh_agents.analysts import Target, run_fundamental
from nivesh_agents.context import RunCtx
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import Call, reply

GOLDEN = Path(__file__).with_name("golden_fa.yaml")
FA_TOOL = "mcp__engine__fa_compute"


def load_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = yaml.safe_load(GOLDEN.read_text())["items"]
    return items


def direction(stance: str) -> str:
    """bullish and bearish count; neutral and insufficient_data are never a direction."""
    return stance if stance in ("bullish", "bearish") else "none"


async def score_golden(run: RunCtx, items: list[dict[str, Any]]) -> int:
    """How many items the fundamental analyst gets in the expected direction (a failed agent is
    a miss, never an error)."""
    hits = 0
    for item in items:
        target = Target(item["security_id"], item["symbol"], "Synthetic " + item["symbol"])
        res = await run_fundamental(run, target)
        stance = getattr(res.output, "stance", "none") if res.status != "failed" else "none"
        hits += direction(stance) == item["expected"]
    return hits


def scripted(items: list[dict[str, Any]], *, invert: bool = False, fail: set[int] = frozenset()):  # type: ignore[no-untyped-def]
    """A fake analyst: answers each golden item from its expected direction (or the opposite)."""
    by_id = {i["security_id"]: i for i in items}

    def respond(call: Call) -> Any:
        sid = json.loads(call.prompt)["security"]["security_id"]
        if sid in fail:
            return reply("not json")
        item = by_id[sid]
        want = item["expected"]
        stance = ("bearish" if want == "bullish" else "bullish") if invert else want
        content = json.dumps({"data": {"subject": item["symbol"], "result": item["summary"]}})
        view = fx.analyst("fundamental", security_id=sid, stance=stance)
        return reply(view, tools=fx.calls(FA_TOOL, 1, 2, 3, content=content))

    return respond
