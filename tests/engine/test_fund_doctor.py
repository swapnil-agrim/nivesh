import dataclasses
import inspect
import json
from datetime import date
from decimal import Decimal
from typing import Any

import nivesh_engine.fund_doctor as fd
from nivesh_core.config import ExitLoad, MfSettings, MfTax
from nivesh_engine.fund_doctor import (
    Action,
    FundFacts,
    Position,
    ReasonCode,
    Verdict,
    diagnose,
)
from nivesh_engine.mf_cost import TwinResult
from nivesh_engine.mf_lots import fifo_lots, valued_lots
from tests.holdings_fx import txn

D = Decimal
CFG = MfSettings()
FOUND = TwinResult("found", "100010", ["100010"])
AS_OF = date(2026, 1, 20)


def facts(**kw: Any) -> FundFacts:
    base: dict[str, Any] = {
        "amfi_code": "100001", "name": "Example Fund", "plan": "direct",
        "beat_pct": D("60"), "median_excess_pct": D("1.5"), "downside_capture_pct": D("90"),
        "max_drawdown_pct": D("20"), "tenure_years": D("5"), "valuation_ratio": D("1.0"),
        "overlap_pct": D("10"),
    }  # fmt: skip
    base.update(kw)
    return FundFacts(**base)


def codes(v: Verdict) -> list[ReasonCode]:
    return [r.code for r in v.reasons]


def position() -> Position:
    t = txn(
        txn_date=date(2025, 12, 1), txn_type="purchase", quantity=D("10"), amount=D("1000"),
        price=None,
    )  # fmt: skip
    return Position(valued_lots(fifo_lots([t]), D("120"), AS_OF, AS_OF, 10), "Equity")


def test_regular_with_twin_and_positive_ter_gap_is_switch_to_direct() -> None:
    v = diagnose(facts(plan="regular", twin=FOUND, ter_gap_pct=D("0.9")), CFG, True, position())
    assert v.action is Action.SWITCH_TO_DIRECT
    r = v.reasons[0]
    assert (r.code, r.metric, r.value, r.threshold) == (
        ReasonCode.TER_GAP_WITH_DIRECT_TWIN, "ter_gap_pct", D("0.9"), D("0")
    )  # fmt: skip


def test_ambiguous_twin_does_not_switch_and_says_why() -> None:
    twin = TwinResult("ambiguous", None, ["100010", "100011"], "ambiguous: 2 direct candidates")
    v = diagnose(facts(plan="regular", twin=twin, ter_gap_pct=D("0.9")), CFG, True)
    assert v.action is Action.REVIEW
    assert ReasonCode.DIRECT_TWIN_AMBIGUOUS in codes(v)
    amb = next(r for r in v.reasons if r.code is ReasonCode.DIRECT_TWIN_AMBIGUOUS)
    assert amb.value == "100010,100011" and "ambiguous" in (amb.note or "")
    assert v.exit_load is None  # not a switch or replace


def test_regular_without_twin_or_gap_does_not_switch() -> None:
    none = TwinResult("none", None, [], "no direct plan")
    assert diagnose(facts(plan="regular", twin=none), CFG, True).action is Action.KEEP
    assert (
        diagnose(facts(plan="regular", twin=FOUND, ter_gap_pct=D("0")), CFG, True).action
        is Action.KEEP
    )
    assert (
        diagnose(facts(plan="direct", twin=FOUND, ter_gap_pct=D("2")), CFG, True).action
        is Action.KEEP
    )


def test_overlap_above_threshold_is_consolidate() -> None:
    v = diagnose(facts(overlap_pct=D("75"), overlap_with="100099"), CFG, True)
    assert v.action is Action.CONSOLIDATE
    assert (v.reasons[0].metric, v.reasons[0].value, v.reasons[0].threshold) == (
        "overlap_pct", D("75"), D("60"),
    )  # fmt: skip
    assert v.reasons[0].note == "100099"


def test_poor_consistency_with_candidate_is_replace() -> None:
    v = diagnose(facts(beat_pct=D("30")), CFG, True, position())
    assert v.action is Action.REPLACE and codes(v) == [ReasonCode.BEAT_PCT_BELOW_MIN]


def test_poor_consistency_without_candidate_is_review() -> None:
    v = diagnose(facts(beat_pct=D("30")), CFG, False, position())
    assert v.action is Action.REVIEW and v.exit_load is None and v.tax_impact is None


def test_manager_change_or_valuation_stretch_is_review() -> None:
    assert diagnose(facts(tenure_years=D("0.5")), CFG, True).action is Action.REVIEW
    v = diagnose(facts(valuation_ratio=D("1.6")), CFG, True)
    assert v.action is Action.REVIEW and codes(v) == [ReasonCode.VALUATION_STRETCH]


def test_clean_fund_is_keep_with_reasons_citing_metrics() -> None:
    v = diagnose(facts(), CFG, True)
    assert v.action is Action.KEEP
    assert {r.metric for r in v.reasons} == {
        "beat_pct", "median_excess_pct", "downside_capture_pct", "max_drawdown_pct",
        "overlap_pct", "tenure_years", "valuation_ratio",
    }  # fmt: skip
    assert all(r.code is ReasonCode.WITHIN_LIMITS and r.threshold is not None for r in v.reasons)


def test_every_reason_has_code_metric_value_threshold() -> None:
    bad = facts(
        beat_pct=D("30"), median_excess_pct=D("-2"), downside_capture_pct=D("120"),
        max_drawdown_pct=D("50"),
    )  # fmt: skip
    v = diagnose(bad, CFG, True)
    assert len(v.reasons) == 4
    for r in v.reasons:
        assert r.code and r.metric and r.value is not None and r.threshold is not None


def test_unavailable_metric_listed_as_unavailable_reason_not_passed() -> None:
    v = diagnose(
        facts(beat_pct=None, unavailable={"beat_pct": "benchmark series missing"}), CFG, True
    )
    assert v.action is Action.REVIEW
    r = next(x for x in v.reasons if x.code is ReasonCode.METRIC_UNAVAILABLE)
    assert (r.metric, r.value, r.note) == ("beat_pct", None, "benchmark series missing")
    keep_like = diagnose(facts(valuation_ratio=None, tenure_years=None), CFG, True)
    assert keep_like.action is Action.KEEP  # non-core metrics are listed but do not force a review
    assert [r.metric for r in keep_like.reasons if r.code is ReasonCode.METRIC_UNAVAILABLE] == [
        "tenure_years", "valuation_ratio",
    ]  # fmt: skip
    assert not any(
        r.metric in ("tenure_years", "valuation_ratio") and r.code is ReasonCode.WITHIN_LIMITS
        for r in keep_like.reasons
    )


def test_idcw_not_comparable_is_review_but_ter_switch_still_applies() -> None:
    nc = facts(
        comparable=False,
        beat_pct=None,
        median_excess_pct=None,
        downside_capture_pct=None,
        max_drawdown_pct=None,
        unavailable={"comparable": "IDCW payouts distort NAV"},
    )
    v = diagnose(nc, CFG, True)
    assert v.action is Action.REVIEW and codes(v) == [ReasonCode.NOT_COMPARABLE]
    sw = diagnose(
        dataclasses.replace(nc, plan="regular", twin=FOUND, ter_gap_pct=D("1")), CFG, True
    )
    assert sw.action is Action.SWITCH_TO_DIRECT


def test_rule_precedence_is_the_documented_order() -> None:
    everything = facts(
        plan="regular", twin=FOUND, ter_gap_pct=D("1"), overlap_pct=D("90"), beat_pct=D("10"),
        tenure_years=D("0.1"),
    )  # fmt: skip
    v = diagnose(everything, CFG, True)
    assert v.action is Action.SWITCH_TO_DIRECT
    assert [r.code for r in v.reasons][:3] == [
        ReasonCode.TER_GAP_WITH_DIRECT_TWIN, ReasonCode.OVERLAP_ABOVE_LIMIT,
        ReasonCode.BEAT_PCT_BELOW_MIN,
    ]  # fmt: skip
    no_switch = dataclasses.replace(everything, plan="direct")
    assert diagnose(no_switch, CFG, True).action is Action.CONSOLIDATE
    no_overlap = dataclasses.replace(no_switch, overlap_pct=D("1"))
    assert diagnose(no_overlap, CFG, True).action is Action.REPLACE
    ok = dataclasses.replace(no_overlap, beat_pct=D("80"))
    assert diagnose(ok, CFG, True).action is Action.REVIEW  # only the short tenure is left


def test_reasons_inside_a_rule_are_alphabetical_by_code() -> None:
    v = diagnose(
        facts(max_drawdown_pct=D("50"), beat_pct=D("10"), downside_capture_pct=D("150")), CFG, True
    )
    assert codes(v) == sorted(codes(v))


def test_thresholds_come_from_config_and_changing_them_changes_the_action() -> None:
    f = facts(beat_pct=D("45"))
    assert diagnose(f, CFG, True).action is Action.REPLACE
    lax = MfSettings.model_validate({"consistency": {"min_beat_pct": 40}})
    assert diagnose(f, lax, True).action is Action.KEEP
    strict = MfSettings.model_validate({"thresholds": {"overlap_pct": 5}})
    assert diagnose(facts(), strict, True).action is Action.CONSOLIDATE


def test_exit_load_and_tax_impact_present_before_action_for_switch_and_replace() -> None:
    cfg = MfSettings(
        exit_load={"Equity": ExitLoad(percent=D("1"), days=365)}, tax=MfTax(long_term_days=365)
    )
    sw = diagnose(facts(plan="regular", twin=FOUND, ter_gap_pct=D("1")), cfg, True, position())
    rp = diagnose(facts(beat_pct=D("10")), cfg, True, position())
    for v in (sw, rp):
        assert (
            v.exit_load is not None
            and v.exit_load.available
            and v.exit_load.amount_inr == D("12.00")
        )
        assert v.tax_impact is not None and v.tax_impact.total_gain == D("200.00")
    names = [f.name for f in dataclasses.fields(Verdict)]
    assert names.index("exit_load") < names.index("tax_impact") < names.index("action")
    no_lots = diagnose(facts(beat_pct=D("10")), cfg, True, None)
    assert no_lots.exit_load is not None and not no_lots.exit_load.available
    assert (
        no_lots.tax_impact is not None and no_lots.tax_impact.reason == "no lot data for this fund"
    )


def test_keep_and_review_do_not_require_exit_load_blocks() -> None:
    for v in (diagnose(facts(), CFG, True), diagnose(facts(tenure_years=D("0.1")), CFG, True)):
        assert v.exit_load is None and v.tax_impact is None


def test_deferred_list_names_st_7_5_agent_and_review_funds() -> None:
    v = diagnose(facts(), CFG, True)
    assert any("ST-7.5" in d and "/review-funds" in d for d in v.deferred)
    v.deferred.append("x")
    assert len(diagnose(facts(), CFG, True).deferred) == 1  # a copy, not the shared constant


def dump(v: Verdict) -> str:
    return json.dumps(dataclasses.asdict(v), default=str, sort_keys=True)


def test_doctor_is_deterministic_byte_identical() -> None:
    f = facts(plan="regular", twin=FOUND, ter_gap_pct=D("1"), beat_pct=D("20"))
    assert dump(diagnose(f, CFG, True, position())) == dump(diagnose(f, CFG, True, position()))


# ---- BR-12: a one-year return is never the reason ---------------------------------------------
def test_top_1y_return_with_poor_consistency_is_not_kept_because_of_1y() -> None:
    star = facts(
        trailing_1y_pct=D("85"), beat_pct=D("20"), median_excess_pct=D("-3"),
        downside_capture_pct=D("140"),
    )  # fmt: skip
    v = diagnose(star, CFG, True, position())
    assert v.action in (Action.REPLACE, Action.REVIEW) and v.action is not Action.KEEP
    assert {r.metric for r in v.reasons} >= {
        "beat_pct",
        "median_excess_pct",
        "downside_capture_pct",
    }


def test_removing_trailing_1y_never_changes_the_action() -> None:
    base = facts(beat_pct=D("20"), downside_capture_pct=D("140"))
    ref = diagnose(base, CFG, True, position())
    for one_year in [None, *(D(x) for x in range(-50, 201, 25))]:
        v = diagnose(dataclasses.replace(base, trailing_1y_pct=one_year), CFG, True, position())
        assert (v.action, v.reasons) == (ref.action, ref.reasons)
    clean = facts()
    for one_year in [None, D("-50"), D("200")]:
        assert (
            diagnose(dataclasses.replace(clean, trailing_1y_pct=one_year), CFG, True).action
            is Action.KEEP
        )


def test_no_verdict_cites_only_a_one_year_return() -> None:
    cases = [
        facts(),
        facts(beat_pct=D("1")),
        facts(plan="regular", twin=FOUND, ter_gap_pct=D("1")),
        facts(overlap_pct=D("99")),
        facts(tenure_years=None),
        facts(comparable=False),
    ]
    for f in cases:
        v = diagnose(dataclasses.replace(f, trailing_1y_pct=D("150")), CFG, True)
        assert v.reasons and all("1y" not in r.metric for r in v.reasons)


def test_doctor_source_does_not_read_trailing_1y() -> None:
    for name, fn in inspect.getmembers(fd, inspect.isfunction):
        if fn.__module__ == fd.__name__:
            assert "trailing_1y" not in inspect.getsource(fn), name
    assert "trailing_1y_pct" in inspect.getsource(fd.FundFacts)  # carried for display only


def test_twin_found_but_ter_gap_unavailable_is_review_not_keep() -> None:
    f = facts(
        plan="regular", twin=FOUND, ter_gap_pct=None, unavailable={"ter_gap_pct": "direct TER"}
    )
    v = diagnose(f, CFG, True)
    assert v.action is Action.REVIEW
    assert [r.metric for r in v.reasons if r.code is ReasonCode.METRIC_UNAVAILABLE] == [
        "ter_gap_pct"
    ]


def test_tenure_years_from_manager_start() -> None:
    from nivesh_engine.fund_doctor import tenure_years

    assert tenure_years(date(2024, 1, 20), date(2026, 1, 20)) == D("2.00")  # 731 days / 365
    assert tenure_years(date(2025, 7, 20), date(2026, 1, 20)) == D("0.50")
    assert tenure_years(None, AS_OF) is None and tenure_years(date(2030, 1, 1), AS_OF) is None
