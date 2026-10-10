import itertools
from dataclasses import replace
from decimal import Decimal

import pytest

from nivesh_core.review_config import ReviewSettings
from nivesh_core.thesis import KillCriterion
from nivesh_engine.review_rules import (
    CODES,
    LADDER,
    TriggerFacts,
    TriggerResult,
    action_floor,
    criterion_status,
    declining_quarters,
    evaluate_triggers,
    merge_criterion,
    sessions_below_sma,
)

D = Decimal
CFG = ReviewSettings()
POS, LONG = "positional_1_6m", "long_term_1y_plus"


def kc(
    comparator: str = "lt", threshold: str = "10", metric: str | None = "roce_pct"
) -> KillCriterion:
    if metric is None:
        return KillCriterion(criterion_id=1, text="text only")
    return KillCriterion.model_validate(
        {
            "criterion_id": 1,
            "text": "t",
            "metric": metric,
            "comparator": comparator,
            "threshold": D(threshold),
        }  # fmt: skip
    )


FACTS = TriggerFacts(
    criteria=((1, "not_met"), (2, "not_met")),
    revenue_growth=(D(10), D(12), D(13)),
    operating_margin=(D(20), D(21), D(22)),
    valuation_percentile=D(50),
    revisions=D(1),
    position_weight_pct=D(5),
    sector_weight_pct=D(10),
    max_position_pct=D(10),
    max_sector_pct=D(30),
    closes=tuple(D(100 + i) for i in range(250)),
    rs_change=D(1),
)
NONE = TriggerFacts(
    criteria=None, revenue_growth=None, operating_margin=None, valuation_percentile=None,
    revisions=None, position_weight_pct=None, sector_weight_pct=None, max_position_pct=D(10),
    max_sector_pct=D(30), closes=None, rs_change=None,
)  # fmt: skip


def by_code(facts: TriggerFacts, horizon: str = POS, cfg: ReviewSettings = CFG) -> dict[str, str]:
    return {t.code: t.status for t in evaluate_triggers(facts, cfg, horizon=horizon)}


def test_criterion_status_for_each_comparator_and_unknown_when_value_missing() -> None:
    cases = [("lt", "9", "met"), ("lt", "10", "not_met"), ("lte", "10", "met"),
             ("lte", "11", "not_met"), ("gt", "11", "met"), ("gt", "10", "not_met"),
             ("gte", "10", "met"), ("gte", "9", "not_met")]  # fmt: skip
    for comparator, value, want in cases:
        assert criterion_status(kc(comparator), D(value)) == want, (comparator, value)
    assert criterion_status(kc(), None) == "unknown"
    assert criterion_status(kc(metric=None), D(1)) == "unknown"


def test_sessions_below_sma_counts_trailing_run_and_none_when_too_few_closes() -> None:
    assert sessions_below_sma([D(1)] * 4, 5) is None
    up = [D(i) for i in range(1, 11)]
    assert sessions_below_sma(up, 3) == 0
    down = [D(10)] * 5 + [D(9), D(8), D(7)]
    assert sessions_below_sma(down, 3) == 3  # last three closes sit under their 3-day mean
    flat = [D(5)] * 6
    assert sessions_below_sma(flat, 3) == 0  # equal to the mean is not below


def test_declining_quarters_needs_n_plus_one_points() -> None:
    assert declining_quarters([D(3), D(2)], 2) is None
    assert declining_quarters([D(3), D(2), D(1)], 2) is True
    assert declining_quarters([D(3), D(2), D(2)], 2) is False
    assert declining_quarters([D(9), D(1), D(3), D(2), D(1)], 2) is True
    assert declining_quarters([D(3), None, D(1)], 2) is None


def test_rule1_kill_criterion() -> None:
    assert by_code(FACTS)["kill_criterion"] == "clear"
    assert by_code(replace(FACTS, criteria=((1, "met"), (2, "unknown"))))["kill_criterion"] == (
        "triggered"
    )
    assert (
        by_code(replace(FACTS, criteria=((1, "not_met"), (2, "unknown"))))["kill_criterion"]
        == "not_evaluable"
    )
    assert by_code(replace(FACTS, criteria=None))["kill_criterion"] == "not_evaluable"


def test_rule2_deterioration_both_series_must_fall() -> None:
    falling = (D(13), D(12), D(10))
    assert by_code(FACTS)["fundamental_deterioration"] == "clear"
    only_rev = replace(FACTS, revenue_growth=falling)
    assert by_code(only_rev)["fundamental_deterioration"] == "clear"
    both = replace(FACTS, revenue_growth=falling, operating_margin=(D(22), D(21), D(20)))
    assert by_code(both)["fundamental_deterioration"] == "triggered"
    short = replace(FACTS, operating_margin=(D(1),))
    assert by_code(short)["fundamental_deterioration"] == "not_evaluable"
    detail = next(t for t in evaluate_triggers(both, CFG, horizon=POS) if t.code == CODES[1])
    assert "prior quarter" in detail.detail and "no plan figures" in detail.detail


def test_rule3_percentile_above_min_with_flat_or_negative_revisions() -> None:
    hot = replace(FACTS, valuation_percentile=D(96))
    assert by_code(hot)["valuation_stretch"] == "clear"  # revisions up
    assert by_code(replace(hot, revisions=D(0)))["valuation_stretch"] == "triggered"
    assert by_code(replace(hot, revisions=D(-1)))["valuation_stretch"] == "triggered"
    assert by_code(replace(hot, revisions=None))["valuation_stretch"] == "not_evaluable"
    edge = replace(FACTS, valuation_percentile=D(95), revisions=D(-1))
    assert by_code(edge)["valuation_stretch"] == "clear"  # must exceed the minimum
    assert by_code(replace(FACTS, valuation_percentile=None))["valuation_stretch"] == (
        "not_evaluable"
    )


def test_rule4_position_or_sector_over_profile_limit() -> None:
    assert by_code(FACTS)["concentration"] == "clear"
    assert by_code(replace(FACTS, position_weight_pct=D("10.01")))["concentration"] == "triggered"
    assert by_code(replace(FACTS, sector_weight_pct=D(31)))["concentration"] == "triggered"
    assert by_code(replace(FACTS, position_weight_pct=D(10)))["concentration"] == "clear"
    unknown = replace(FACTS, sector_weight_pct=None)
    assert by_code(unknown)["concentration"] == "not_evaluable"
    over_and_unknown = replace(unknown, position_weight_pct=D(12))
    assert by_code(over_and_unknown)["concentration"] == "triggered"


def test_rule5_only_for_positional_horizon_else_not_applicable() -> None:
    weak = tuple(D(300 - i) for i in range(260))
    facts = replace(FACTS, closes=weak, rs_change=D(-1))
    assert by_code(facts, POS)["below_sma200_weak_rs"] == "triggered"
    assert by_code(facts, LONG)["below_sma200_weak_rs"] == "not_applicable"
    assert by_code(replace(facts, rs_change=D(0)), POS)["below_sma200_weak_rs"] == "clear"
    assert by_code(replace(facts, rs_change=None), POS)["below_sma200_weak_rs"] == "not_evaluable"
    assert by_code(FACTS, POS)["below_sma200_weak_rs"] == "clear"  # rising closes
    few = replace(facts, closes=weak[:100])
    assert by_code(few, POS)["below_sma200_weak_rs"] == "not_evaluable"


def test_rule6_always_not_evaluable_until_idea_runs() -> None:
    for facts in (FACTS, NONE):
        t = next(x for x in evaluate_triggers(facts, CFG, horizon=POS) if x.code == CODES[5])
        assert t.status == "not_evaluable" and "ST-9.4" in t.detail


def test_missing_inputs_are_never_clear() -> None:
    out = evaluate_triggers(NONE, CFG, horizon=POS)
    assert [t.code for t in out] == list(CODES)
    assert all(t.status == "not_evaluable" for t in out), out
    assert all(t.detail for t in out)


def test_thresholds_come_from_config() -> None:
    hot = replace(FACTS, valuation_percentile=D(91), revisions=D(0))
    assert by_code(hot)["valuation_stretch"] == "clear"
    assert (
        by_code(hot, cfg=ReviewSettings(valuation_percentile_min=D(90)))["valuation_stretch"]
        == "triggered"
    )
    weak = replace(FACTS, closes=tuple(D(300 - i) for i in range(230)), rs_change=D(-1))
    assert by_code(weak)["below_sma200_weak_rs"] == "clear"  # 30 sessions < 60
    assert (
        by_code(weak, cfg=ReviewSettings(below_sma200_sessions=30))["below_sma200_weak_rs"]
        == "triggered"
    )
    three = replace(
        FACTS, revenue_growth=(D(9), D(13), D(12), D(10)), operating_margin=(D(9), D(3), D(2), D(1))
    )
    assert by_code(three)["fundamental_deterioration"] == "triggered"
    assert (
        by_code(three, cfg=ReviewSettings(deterioration_quarters=3))["fundamental_deterioration"]
        == "clear"
    )


def clear_triggers(**over: str) -> tuple[TriggerResult, ...]:
    return tuple(TriggerResult(c, over.get(c, "clear"), "d") for c in CODES)


def test_floor_met_criterion_without_override_gives_trim_or_exit_never_hold() -> None:
    for proposed in LADDER:
        action, notes = action_floor(
            proposed, [(1, "met"), (2, "not_met")], clear_triggers(kill_criterion="triggered"),
            override_reason="", review_due=False,
        )  # fmt: skip
        assert action in ("TRIM", "EXIT"), proposed
    action, notes = action_floor(
        "HOLD", [(1, "met")], clear_triggers(), override_reason="", review_due=False
    )
    assert action == "TRIM" and any("HOLD" in n and "TRIM" in n for n in notes)


def test_floor_met_criterion_with_override_reason_gives_review_never_hold_or_add() -> None:
    action, notes = action_floor(
        "HOLD", [(1, "met")], clear_triggers(), override_reason="one-off charge", review_due=False
    )
    assert action == "REVIEW" and any("one-off charge" in n for n in notes)
    action, _ = action_floor(
        "ADD", [(1, "not_met")], clear_triggers(concentration="triggered"),
        override_reason="x", review_due=False,
    )  # fmt: skip
    assert action == "REVIEW"
    action, _ = action_floor(
        "EXIT", [(1, "met")], clear_triggers(), override_reason="x", review_due=False
    )
    assert action == "EXIT"


@pytest.mark.parametrize("code", ["fundamental_deterioration", "valuation_stretch",
                                  "below_sma200_weak_rs"])  # fmt: skip
def test_floor_rules_2_3_5_and_review_due_cap_at_review(code: str) -> None:
    for proposed, want in (("ADD", "REVIEW"), ("HOLD", "REVIEW"), ("TRIM", "TRIM")):
        got, _ = action_floor(
            proposed, [], clear_triggers(**{code: "triggered"}), override_reason="",
            review_due=False,
        )  # fmt: skip
        assert got == want
    got, notes = action_floor("HOLD", [], clear_triggers(), override_reason="", review_due=True)
    assert got == "REVIEW" and any("review date" in n for n in notes)


def test_floor_never_raises_the_proposed_action() -> None:
    for proposed in LADDER:
        got, notes = action_floor(
            proposed, [(1, "not_met")], clear_triggers(), override_reason="", review_due=False
        )
        assert got == proposed and notes == ()


def test_floor_table_all_actions_times_criterion_status_times_triggers_times_override() -> None:
    statuses = ("met", "not_met", "unknown")
    trigger_states = ("triggered", "clear", "not_evaluable")
    for proposed, crit, t1, t4, t2, overr, due in itertools.product(
        LADDER, statuses, trigger_states, trigger_states, trigger_states, ("", "why"),
        (False, True),
    ):  # fmt: skip
        trig = clear_triggers(kill_criterion=t1, concentration=t4, fundamental_deterioration=t2)
        got, _ = action_floor(proposed, [(1, crit)], trig, override_reason=overr, review_due=due)
        assert LADDER.index(got) >= LADDER.index(proposed)  # code only lowers
        hard = crit == "met" or "triggered" in (t1, t4)
        if hard:
            assert got not in ("HOLD", "ADD")
            if not overr:
                assert got in ("TRIM", "EXIT")
        elif t2 == "triggered" or due:
            assert got not in ("HOLD", "ADD")
        else:
            assert got == proposed


def test_code_value_overrides_agent_status_and_is_logged() -> None:
    status, note = merge_criterion(kc("lt", "10"), D(5), "not_met")
    assert status == "met" and note is not None and "criterion 1" in note
    status, note = merge_criterion(kc("lt", "10"), D(15), "met")
    assert status == "not_met" and note is not None
    assert merge_criterion(kc("lt", "10"), D(5), "met") == ("met", None)


def test_unknown_code_value_keeps_agent_met_and_downgrades_agent_not_met_to_unknown() -> None:
    assert merge_criterion(kc(), None, "met") == ("met", None)
    status, note = merge_criterion(kc(), None, "not_met")
    assert status == "unknown" and note is not None
    assert merge_criterion(kc(), None, "unknown") == ("unknown", None)
    text_only = kc(metric=None)
    assert merge_criterion(text_only, None, "not_met") == ("not_met", None)
    assert merge_criterion(text_only, D(1), "met") == ("met", None)
