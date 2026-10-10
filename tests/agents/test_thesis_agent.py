"""Thesis drafting for onboarding (ST-8.1) with a faked SDK: no network, no model, no holdings."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nivesh_agents import runtime
from nivesh_agents.analysts import Target
from nivesh_agents.schemas import ThesisDraft
from nivesh_agents.store import RunStore
from nivesh_agents.thesis_agent import (
    Held,
    apply_choice,
    draft_all,
    draft_thesis,
    merge_held,
    onboard_targets,
)
from nivesh_core.thesis import Thesis
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import Call, reply

D = Decimal


def sid_of(call: Call) -> int:
    body = json.loads(call.prompt)
    return int(body["security_id"] if "security_id" in body else body["security"]["security_id"])


def good_draft(call: Call) -> Any:
    return reply(fx.thesis_draft(security_id=sid_of(call)))


def store_at(tmp_path: Path, run: Any) -> RunStore:
    d = tmp_path / "run"
    d.mkdir()
    return RunStore(d, run.tracer)


def draft_model(**over: Any) -> ThesisDraft:
    return ThesisDraft.model_validate_json(json.dumps(fx.thesis_draft(**over)))


async def test_draft_runs_three_analysts_then_one_draft_call_with_no_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fx.happy(thesis_draft=good_draft)
    monkeypatch.setattr(runtime, "query", fake)
    run = fx.run_ctx(tmp_path)
    out = await draft_thesis(run, fx.TARGET, store_at(tmp_path, run))
    assert isinstance(out.draft, ThesisDraft) and out.reason is None and out.gaps == ()
    assert fake.counts == {"fundamental": 1, "technical": 1, "news": 1, "thesis_draft": 1}
    last = fake.seen[-1]
    assert last.agent == "thesis_draft"
    assert last.options.allowed_tools == [] and last.options.mcp_servers == {}
    body = json.loads(last.prompt)
    assert sorted(body["views"]) == ["fundamental", "news", "technical"]


async def test_draft_evidence_must_point_into_the_given_views_else_one_repair_then_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = fx.thesis_draft(evidence=[{"view": "fundamental", "point_index": 9}])
    fake = fx.happy(thesis_draft=[reply(bad), reply(bad)])
    monkeypatch.setattr(runtime, "query", fake)
    run = fx.run_ctx(tmp_path)
    out = await draft_thesis(run, fx.TARGET, store_at(tmp_path, run))
    assert out.draft is None and out.reason is not None and "does not exist" in out.reason
    assert fake.counts["thesis_draft"] == 2

    fake2 = fx.happy(thesis_draft=[reply(bad), reply(fx.thesis_draft())])
    monkeypatch.setattr(runtime, "query", fake2)
    run2 = fx.run_ctx(tmp_path / "b")
    (tmp_path / "b").mkdir()
    assert (await draft_thesis(run2, fx.TARGET, store_at(tmp_path / "b", run2))).draft


async def test_draft_for_wrong_security_id_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = reply(fx.thesis_draft(security_id=2))
    monkeypatch.setattr(runtime, "query", fx.happy(thesis_draft=[wrong, wrong]))
    run = fx.run_ctx(tmp_path)
    out = await draft_thesis(run, fx.TARGET, store_at(tmp_path, run))
    assert out.draft is None and "security_id must be 1" in (out.reason or "")


async def test_failed_analysts_still_draft_with_data_gaps_or_skip_when_all_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fx.happy(news=lambda c: RuntimeError("feed down"), thesis_draft=good_draft)
    monkeypatch.setattr(runtime, "query", fake)
    run = fx.run_ctx(tmp_path)
    out = await draft_thesis(run, fx.TARGET, store_at(tmp_path, run))
    assert out.draft is not None and [g.split(":")[0] for g in out.gaps] == ["news"]
    body = json.loads(fake.calls("thesis_draft")[0].prompt)
    assert "news" not in body["views"] and body["data_gaps"][0].startswith("news:")

    down = lambda c: RuntimeError("down")  # noqa: E731
    fake2 = fx.happy(fundamental=down, technical=down, news=down, thesis_draft=good_draft)
    monkeypatch.setattr(runtime, "query", fake2)
    (tmp_path / "b").mkdir()
    run2 = fx.run_ctx(tmp_path / "b")
    out2 = await draft_thesis(run2, fx.TARGET, store_at(tmp_path / "b", run2))
    assert out2.draft is None and "no analyst view" in (out2.reason or "")
    assert "thesis_draft" not in fake2.counts


async def test_draft_failure_marks_holding_skipped_and_others_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def draft(call: Call) -> Any:
        return RuntimeError("model down") if sid_of(call) == 1 else good_draft(call)

    monkeypatch.setattr(runtime, "query", fx.happy(thesis_draft=draft))
    run = fx.run_ctx(tmp_path)
    targets = [fx.TARGET, Target(2, "US2", "Example Tech 2")]
    outs = await draft_all(run, targets, store_at(tmp_path, run))
    assert [o.target.security_id for o in outs] == [1, 2]
    assert outs[0].draft is None and "model down" in (outs[0].reason or "")
    assert outs[1].draft is not None and outs[1].draft.security_id == 2


async def test_draft_all_turns_an_unexpected_error_into_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nivesh_agents.thesis_agent as ta

    async def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("bug")

    monkeypatch.setattr(ta, "draft_thesis", boom)
    run = fx.run_ctx(tmp_path)
    (out,) = await draft_all(run, [fx.TARGET], store_at(tmp_path, run))
    assert out.draft is None and out.reason == "ValueError"


def held(sid: int, symbol: str, value: Any, kind: str = "equity") -> Held:
    return Held(sid, symbol, kind, None if value is None else D(value))


def test_targets_are_equity_and_etf_holdings_without_an_active_thesis_sorted_by_value() -> None:
    rows = [
        held(1, "AAA", 100), held(2, "BBB", 900, "etf"), held(3, "CCC", 500),
        held(4, "FND", 9000, "mf"), held(5, "DDD", None),
    ]  # fmt: skip
    targets = onboard_targets(rows, covered={3})
    assert [t.symbol for t in targets] == ["BBB", "AAA", "DDD"]
    assert targets[0] == Target(2, "BBB", "BBB", "etf", "IN")


def test_targets_dedupe_one_security_held_in_two_accounts() -> None:
    rows = [held(1, "AAA", 100), held(2, "BBB", 150), held(1, "AAA", 100)]
    merged = merge_held(rows)
    assert [(h.security_id, h.value_inr) for h in merged] == [(1, D(200)), (2, D(150))]
    assert [t.security_id for t in onboard_targets(rows, covered=set())] == [1, 2]
    assert merge_held([held(1, "AAA", 100), held(1, "AAA", None)])[0].value_inr is None


CREATED = date(2026, 10, 1)


def test_apply_choice_accept_builds_thesis_with_created_and_90_day_review_date() -> None:
    t = apply_choice(draft_model(), "accept", None, created=CREATED, review_days=90)
    assert isinstance(t, Thesis)
    assert (t.created_at, t.review_date) == (CREATED, date(2026, 12, 30))
    assert t.security_id == 1 and t.source == "onboarding" and t.status == "active"
    assert t.kill_criteria[0].threshold == D("12.5")


def test_apply_choice_edit_replaces_fields_and_revalidates() -> None:
    crit = [fx.kill(1), {**fx.kill(2), "threshold": "30", "comparator": "gt", "metric": "pe"}]
    edits = {"why": "A shorter reason.", "horizon": "positional_1_6m", "kill_criteria": crit}
    t = apply_choice(draft_model(), "edit", edits, created=CREATED, review_days=30)
    assert isinstance(t, Thesis)
    assert t.why == "A shorter reason." and t.horizon == "positional_1_6m"
    assert t.kill_criteria[1].metric == "pe" and t.kill_criteria[1].threshold == D(30)
    assert t.review_date == date(2026, 10, 31)


def test_apply_choice_invalid_edit_returns_errors_not_a_thesis() -> None:
    errors = apply_choice(
        draft_model(), "edit", {"why": "word " * 61}, created=CREATED, review_days=90
    )
    assert isinstance(errors, list) and any("60 words" in e for e in errors)
    errors = apply_choice(
        draft_model(), "edit", {"kill_criteria": [fx.kill(1)]}, created=CREATED, review_days=90
    )
    assert isinstance(errors, list) and errors


def test_apply_choice_skip_returns_none() -> None:
    assert apply_choice(draft_model(), "skip", None, created=CREATED, review_days=90) is None


async def test_draft_outputs_are_saved_to_the_run_dir_before_the_owner_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def draft(call: Call) -> Any:
        return RuntimeError("down") if sid_of(call) == 2 else good_draft(call)

    monkeypatch.setattr(runtime, "query", fx.happy(thesis_draft=draft))
    run = fx.run_ctx(tmp_path)
    await draft_all(run, [fx.TARGET, Target(2, "US2", "Example Tech 2")], store_at(tmp_path, run))
    files = sorted(p.name.split("_", 1)[1] for p in (tmp_path / "run" / "outputs").iterdir())
    assert "thesis_draft_1.json" in files and "thesis_draft_2.json" in files
    assert {"fundamental_1.json", "technical_1.json", "news_1.json"} <= set(files)
    saved = {p.name.split("_", 1)[1]: p for p in (tmp_path / "run" / "outputs").iterdir()}
    assert json.loads(saved["thesis_draft_1.json"].read_text())["security_id"] == 1
    assert json.loads(saved["thesis_draft_2.json"].read_text())["status"] == "failed"


async def test_prompt_carries_identifiers_only_no_quantities_values_or_holder_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fx.happy(thesis_draft=good_draft)
    monkeypatch.setattr(runtime, "query", fake)
    run = fx.run_ctx(tmp_path)
    await draft_thesis(run, fx.TARGET, store_at(tmp_path, run))
    body = json.loads(fake.calls("thesis_draft")[0].prompt)
    assert set(body) == {
        "task", "as_of", "security_id", "symbol", "name", "kind", "market", "views", "data_gaps",
    }  # fmt: skip
    text = json.dumps(body)
    for word in ("quantity", "value_inr", "holder", "account", "avg_cost"):
        assert word not in text


async def test_draft_with_a_metric_unknown_to_engines_is_repaired_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    typo = {**fx.kill(1), "metric": "roce_percent"}
    bad = fx.thesis_draft(kill_criteria=[typo, fx.kill(2, machine=False)])
    fake = fx.happy(thesis_draft=[reply(bad), reply(fx.thesis_draft())])
    monkeypatch.setattr(runtime, "query", fake)
    run = fx.run_ctx(tmp_path)
    out = await draft_thesis(run, fx.TARGET, store_at(tmp_path, run))
    assert out.draft is not None and fake.counts["thesis_draft"] == 2
    assert "roce_percent is not an engine metric" in fake.seen[-1].prompt


def test_apply_choice_rejects_a_metric_unknown_to_engines() -> None:
    crit = [{**fx.kill(1), "metric": "roce_percent"}, fx.kill(2, machine=False)]
    errors = apply_choice(
        draft_model(), "edit", {"kill_criteria": crit}, created=CREATED, review_days=90
    )
    assert isinstance(errors, list)
    assert any("roce_percent is not an engine metric" in e for e in errors)


def test_known_metrics_match_what_review_facts_produce_under_the_project_config() -> None:
    from nivesh_core.config import load_settings
    from nivesh_engine.metrics import Bundles, SecurityInputs, known_metrics, metric_values
    from nivesh_engine.valuation import ValuationInputs

    cfg = load_settings(Path(__file__).parents[2] / "config" / "nivesh.yaml").analysis
    day = date(2026, 1, 2)
    inp = SecurityInputs(1, "X", valuation=ValuationInputs("IN", "general", day, (), (), None))
    assert set(metric_values(Bundles(inp, day, cfg))) == known_metrics()
    assert {"roce_pct", "debt_equity", "pe_percentile_5y"} <= known_metrics()
