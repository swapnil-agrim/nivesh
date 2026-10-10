import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nivesh_agents import runtime
from nivesh_agents.debate import run_debate
from nivesh_agents.lenses import disagreement_count, lenses_enabled, run_lenses
from nivesh_agents.schemas import LensView
from nivesh_core.agents_config import AgentsSettings
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import reply, script

D = Decimal


def bull(rnd: int, **over: Any) -> dict[str, Any]:
    return fx.turn("bull", rnd, **over)


def bear(rnd: int, **over: Any) -> dict[str, Any]:
    return fx.turn("bear", rnd, **over)


def last(**over: Any) -> dict[str, Any]:
    return {"strongest_unrebutted": "Margins may not recover", **over}


def deep_fake(rounds: int = 2) -> Any:
    """Bull and bear scripts for `rounds` rounds; the last round states the strongest point."""
    return script(
        bull=[reply(bull(r, **(last() if r == rounds else {}))) for r in range(1, rounds + 1)],
        bear=[reply(bear(r, **(last() if r == rounds else {}))) for r in range(1, rounds + 1)],
    )


def ctx(tmp_path: Path, mode: str = "deep", **cfg: Any) -> Any:
    run = fx.run_ctx(tmp_path, mode, AgentsSettings(**cfg))
    fx.register_analyst_calls(run, "fundamental", "technical", "news")
    return run


async def test_default_two_rounds_produce_four_turns_bull_then_bear_alternating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = deep_fake(2)
    monkeypatch.setattr(runtime, "query", fake)
    d = await run_debate(ctx(tmp_path), fx.TARGET, fx.views())
    assert [(t.side, t.round) for t in d.turns] == [
        ("bull", 1), ("bear", 1), ("bull", 2), ("bear", 2),
    ]  # fmt: skip
    assert [c.agent for c in fake.seen] == ["bull", "bear", "bull", "bear"] and not d.partial


async def test_rounds_one_and_three_are_honoured_and_zero_or_four_is_rejected_by_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in (1, 3):
        fake = deep_fake(n)
        monkeypatch.setattr(runtime, "query", fake)
        d = await run_debate(ctx(tmp_path, debate_rounds=n), fx.TARGET, fx.views())
        assert len(d.turns) == 2 * n
    for bad in (0, 4):
        with pytest.raises(ValueError, match="debate_rounds"):
            AgentsSettings(debate_rounds=bad)


async def test_each_turn_sees_the_prior_turns_in_its_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = deep_fake(2)
    monkeypatch.setattr(runtime, "query", fake)
    await run_debate(ctx(tmp_path), fx.TARGET, fx.views())
    prior = [len(json.loads(c.prompt)["prior_turns"]) for c in fake.seen]
    assert prior == [0, 1, 2, 3]
    third = json.loads(fake.seen[2].prompt)
    assert [t["side"] for t in third["prior_turns"]] == ["bull", "bear"]
    assert set(third["views"]) == {"fundamental", "technical", "news"}


async def test_debate_turns_use_no_tools_and_no_servers_and_the_fake_sees_zero_tool_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = deep_fake(2)
    monkeypatch.setattr(runtime, "query", fake)
    run = ctx(tmp_path)
    before = run.tracer.all_tool_ids()
    await run_debate(run, fx.TARGET, fx.views())
    for c in fake.seen:
        assert c.options.allowed_tools == [] and c.options.mcp_servers == {}
    assert run.tracer.all_tool_ids() == before  # no new tool calls were made


async def test_every_cited_view_pointer_must_exist_in_the_views_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = {"claim": "c", "evidence": [{"view": "macro", "point_index": 0}]}
    far = {"claim": "c", "evidence": [{"view": "news", "point_index": 9}]}
    for claims in ([ghost], [far]):
        fake = script(
            bull=[reply(bull(1, claims=claims)), reply(bull(1, claims=claims))],
            bear=[reply(bear(1, summary=True)), ],
        )  # fmt: skip
        monkeypatch.setattr(runtime, "query", fake)
        d = await run_debate(ctx(tmp_path, "quick"), fx.TARGET, fx.views())
        assert d.partial and "does not exist" in d.results[0].reason


async def test_citing_a_new_tool_call_id_is_invalid_and_repaired_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new = {"claim": "c", "evidence": [{"tool_call_id": fx.tid(88)}]}
    fixed = bull(1, summary=True)
    fake = script(
        bull=[reply(bull(1, summary=True, claims=[new])), reply(fixed)],
        bear=[reply(bear(1, summary=True))],
    )  # fmt: skip
    monkeypatch.setattr(runtime, "query", fake)
    d = await run_debate(ctx(tmp_path, "quick"), fx.TARGET, fx.views())
    assert not d.partial and fake.counts["bull"] == 2
    assert d.results[0].status == "repaired"
    known = {"claim": "c", "evidence": [{"tool_call_id": fx.tid(1)}]}  # an analyst's real id
    ok = script(
        bull=[reply(bull(1, summary=True, claims=[known]))], bear=[reply(bear(1, summary=True))]
    )
    monkeypatch.setattr(runtime, "query", ok)
    (tmp_path / "x").mkdir()
    assert not (await run_debate(ctx(tmp_path / "x", "quick"), fx.TARGET, fx.views())).partial


async def test_strongest_unrebutted_point_is_stated_after_the_last_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = deep_fake(2)
    monkeypatch.setattr(runtime, "query", fake)
    d = await run_debate(ctx(tmp_path), fx.TARGET, fx.views())
    assert d.strongest("bull") == "Margins may not recover" == d.strongest("bear")
    assert [json.loads(c.prompt)["final_round"] for c in fake.seen] == [False, False, True, True]
    # a final turn without it is invalid and repaired once
    silent = script(
        bull=[reply(bull(1)), reply(bull(1, **last()))], bear=[reply(bear(1, **last()))]
    )
    monkeypatch.setattr(runtime, "query", silent)
    one = await run_debate(ctx(tmp_path, debate_rounds=1), fx.TARGET, fx.views())
    assert one.results[0].status == "repaired"


async def test_quick_mode_yields_one_bull_and_one_bear_summary_and_no_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(bull=[reply(bull(1, summary=True))], bear=[reply(bear(1, summary=True))])
    monkeypatch.setattr(runtime, "query", fake)
    d = await run_debate(ctx(tmp_path, "quick"), fx.TARGET, fx.views())
    assert sorted((t.side, t.round, t.summary) for t in d.turns) == [
        ("bear", 1, True), ("bull", 1, True),
    ]  # fmt: skip
    assert len(fake.seen) == 2


async def test_transcript_is_collected_in_order_for_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", deep_fake(2))
    d = await run_debate(ctx(tmp_path), fx.TARGET, fx.views())
    dumped = [t.model_dump(mode="json") for t in d.turns]
    assert [x["side"] for x in dumped] == ["bull", "bear", "bull", "bear"]
    assert [x["round"] for x in dumped] == [1, 1, 2, 2]
    json.dumps(dumped)  # storable


async def test_failed_debate_turn_does_not_stop_the_run_and_marks_the_debate_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = script(
        bull=[RuntimeError("down"), reply(bull(2, **last()))],
        bear=[reply(bear(1)), reply(bear(2, **last()))],
    )  # fmt: skip
    monkeypatch.setattr(runtime, "query", fake)
    d = await run_debate(ctx(tmp_path), fx.TARGET, fx.views())
    assert d.partial and "bull round 1" in d.failures[0]
    assert [(t.side, t.round) for t in d.turns] == [("bear", 1), ("bull", 2), ("bear", 2)]


# ---- lenses -----------------------------------------------------------------------------------
LENS_NAMES = ("value", "growth", "contrarian", "valuation")


def lens_fake(decisions: dict[str, str] | None = None, fail: set[str] | None = None) -> Any:
    d = decisions or {n: "would_buy" for n in LENS_NAMES}
    by = {
        f"lens_{n}": [reply("nope"), reply("nope")]
        if n in (fail or set())
        else [reply(fx.lens(n, d[n]))]
        for n in LENS_NAMES
    }
    return script(**by)


async def test_lens_agents_run_only_in_deep_mode_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = lens_fake()
    monkeypatch.setattr(runtime, "query", fake)
    assert lenses_enabled(ctx(tmp_path, "deep")) and not lenses_enabled(ctx(tmp_path, "quick"))
    quick = await run_lenses(ctx(tmp_path, "quick"), fx.TARGET, fx.views())
    off = await run_lenses(ctx(tmp_path, "deep", lenses_enabled=False), fx.TARGET, fx.views())
    assert quick.views == [] and off.views == [] and fake.seen == []
    deep = await run_lenses(ctx(tmp_path, "deep"), fx.TARGET, fx.views())
    assert len(deep.views) == 4 and len(fake.seen) == 4


async def test_each_enabled_lens_returns_a_schema_valid_lens_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", lens_fake())
    out = await run_lenses(ctx(tmp_path, lenses=["value", "growth"]), fx.TARGET, fx.views())
    assert [v.lens for v in out.views] == ["value", "growth"]
    assert all(isinstance(v, LensView) for v in out.views)


async def test_lens_agents_have_no_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = lens_fake()
    monkeypatch.setattr(runtime, "query", fake)
    await run_lenses(ctx(tmp_path), fx.TARGET, fx.views())
    assert all(c.options.allowed_tools == [] and c.options.mcp_servers == {} for c in fake.seen)


def lv(decision: str) -> LensView:
    return LensView.model_validate_json(json.dumps(fx.lens("value", decision)))


def test_disagreement_count_counts_lenses_that_differ_from_the_final_upward_or_not_verdict() -> (
    None
):
    views = [lv("would_buy"), lv("would_buy"), lv("would_pass")]
    assert disagreement_count(views, "BUY") == 1 and disagreement_count(views, "ACCUMULATE") == 1
    assert disagreement_count(views, "HOLD") == 2 and disagreement_count(views, "SELL") == 2
    assert disagreement_count(views, "INSUFFICIENT_DATA") == 2
    assert disagreement_count([], "BUY") == 0


def test_disagreement_count_is_computed_after_the_verdict_in_code() -> None:
    import inspect

    import nivesh_agents.lenses as lenses_mod

    params = list(inspect.signature(disagreement_count).parameters)
    assert params == ["lens_views", "final_verdict"]  # needs the final verdict, so it comes last
    src = inspect.getsource(lenses_mod)
    assert "pm" not in src.lower().replace("params", "")  # nothing here talks to the PM agent


async def test_flipping_every_lens_output_leaves_the_verdict_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nivesh_engine.committee_rules import verdict_ceiling

    def verdict_for(decision: str) -> str:
        return verdict_ceiling(
            "ACCUMULATE", veto=False, card_band="upper", card_cap=None, hard_flag=False,
            coverage=D(100), min_coverage=D(70),
        ).verdict  # fmt: skip

    outs = []
    for d in ("would_buy", "would_pass"):
        monkeypatch.setattr(runtime, "query", lens_fake({n: d for n in LENS_NAMES}))
        got = await run_lenses(ctx(tmp_path), fx.TARGET, fx.views())
        outs.append((got, verdict_for(d)))
    assert outs[0][1] == outs[1][1] == "ACCUMULATE"
    assert disagreement_count(outs[0][0].views, "ACCUMULATE") == 0
    assert disagreement_count(outs[1][0].views, "ACCUMULATE") == 4


async def test_lens_failure_is_excluded_from_the_count_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", lens_fake(fail={"growth"}))
    out = await run_lenses(ctx(tmp_path), fx.TARGET, fx.views())
    assert len(out.views) == 3 and len(out.failures) == 1 and "lens growth" in out.failures[0]
    assert disagreement_count(out.views, "HOLD") == 3
