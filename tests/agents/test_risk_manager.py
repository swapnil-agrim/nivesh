import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nivesh_agents import runtime
from nivesh_agents.risk_manager import UNAVAILABLE, facts_json, merge_assessment, run_risk
from nivesh_engine.committee_rules import risk_veto, weight_bounds
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import reply, script

D = Decimal
RISK_TOOL = "mcp__engine__risk_metrics"


def draft(**over: Any) -> dict[str, Any]:
    return fx.risk(**over)


async def go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: list[Any],
    facts: Any = None,
    **kw: Any,
) -> Any:
    fake = script(risk=attempts)
    monkeypatch.setattr(runtime, "query", fake)
    out = await run_risk(
        fx.run_ctx(tmp_path), fx.TARGET, facts or fx.facts(), max_days_to_trade=kw.get("max")
    )
    return out, fake


async def test_risk_agent_allow_list_is_risk_metrics_xray_flags_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = await go(tmp_path, monkeypatch, [reply(draft())])
    o = fake.seen[0].options
    assert set(o.allowed_tools) == {
        "mcp__engine__risk_metrics", "mcp__engine__portfolio_xray", "mcp__engine__red_flags",
    }  # fmt: skip
    assert set(o.mcp_servers) == {"engine"}  # type: ignore[arg-type]


async def test_model_veto_false_is_overwritten_by_the_rule_veto_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, _ = await go(tmp_path, monkeypatch, [reply(draft(veto=False))], fx.facts(hard_flag=True))
    a = out.assessment
    assert a.veto is True and a.veto_reasons == ["hard_red_flag"] and out.veto.veto is True
    assert any(o.startswith("veto: model said False, rules say True") for o in a.overrides)


async def test_model_weight_above_bound_is_clamped_and_logged_in_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wide = draft(weight_bounds={"min_pct": 0, "max_pct": 50})
    out, _ = await go(tmp_path, monkeypatch, [reply(wide)], fx.facts(sector_headroom_pct=D(6)))
    assert out.assessment.weight_bounds.max_pct == D(6) and out.bounds.max_pct == D(6)
    assert "weight_bounds.max_pct 50 -> 6" in out.assessment.overrides


async def test_model_cannot_clear_a_veto_and_may_add_a_soft_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soft = draft(veto=False, risks=["thin float; watch gaps"], correlation_note="moves with banks")
    out, _ = await go(tmp_path, monkeypatch, [reply(soft)], fx.facts(excluded=True))
    a = out.assessment
    assert a.veto is True and "thin float; watch gaps" in a.risks
    assert a.correlation_note == "moves with banks"
    ok, _ = await go(tmp_path, monkeypatch, [reply(draft(veto=True))], fx.facts())
    assert ok.assessment.veto is False  # the model cannot add a veto either: rules decide


async def test_assessment_has_liquidity_and_correlation_notes_from_engine_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blank = draft(liquidity_note="", correlation_note="", concentration_note="")
    out, fake = await go(tmp_path, monkeypatch, [reply(blank)], fx.facts(days_to_trade=D("3.5")))
    a = out.assessment
    assert "3.5" in a.liquidity_note and a.correlation_note and "2%" in a.concentration_note
    sent = json.loads(fake.seen[0].prompt)["facts"]
    assert sent["days_to_trade"] == "3.5" and sent["tested_weight_pct"] == "2"
    unknown, _ = await go(tmp_path, monkeypatch, [reply(blank)], fx.facts(days_to_trade=None))
    assert "could not be computed" in unknown.assessment.liquidity_note


async def test_profile_limits_come_from_the_profile_not_from_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    greedy = draft(weight_bounds={"min_pct": 0, "max_pct": 99})
    out, fake = await go(
        tmp_path,
        monkeypatch,
        [reply(greedy)],
        fx.facts(max_position_pct=D(7), sector_headroom_pct=None),
    )
    assert out.assessment.weight_bounds.max_pct == D(7)
    body = json.loads(fake.seen[0].prompt)["facts"]
    assert body["profile_max_position_pct"] == "7" and body["profile_max_sector_pct"] == "30"


async def test_risk_view_failure_gives_a_vetoed_unavailable_assessment_so_the_pm_cannot_go_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, fake = await go(tmp_path, monkeypatch, [reply("nonsense"), reply("still nonsense")])
    a = out.assessment
    assert out.result.status == "failed" and fake.counts == {"risk": 2}
    assert a.veto is True and a.veto_reasons[0] == UNAVAILABLE
    assert a.security_id == fx.TARGET.security_id and out.veto.veto
    clean, _ = await go(tmp_path, monkeypatch, [RuntimeError("down")], fx.facts(excluded=True))
    assert clean.assessment.veto_reasons == [UNAVAILABLE, "excluded_by_profile"]


async def test_risk_output_is_schema_valid_and_evidence_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = draft(security_id=99)
    out, fake = await go(
        tmp_path, monkeypatch,
        [reply(wrong, tools=fx.calls(RISK_TOOL, 1)), reply(draft())],
    )  # fmt: skip
    assert out.result.status == "repaired" and "security_id must be 1" in fake.seen[1].prompt
    assert out.assessment.security_id == 1
    bad_type, _ = await go(tmp_path, monkeypatch, [reply(draft(veto="yes")), reply(draft())])
    assert bad_type.result.status == "repaired"


def test_merge_without_a_draft_and_helpers_are_pure() -> None:
    f = fx.facts(days_to_trade=D(2))
    v = risk_veto(f)
    a = merge_assessment(None, f, v, weight_bounds(f))
    assert a.veto is True and a.veto_reasons == [UNAVAILABLE]
    assert facts_json(f)["days_to_trade"] == "2"
    assert facts_json(fx.facts(sector_headroom_pct=None))["sector_headroom_pct"] is None
