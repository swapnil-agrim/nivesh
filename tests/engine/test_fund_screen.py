import dataclasses
import json
from datetime import date
from decimal import Decimal
from typing import Any

from nivesh_core.config import MfScreenWeights
from nivesh_core.mf_models import FundHoldingRow
from nivesh_engine.fund_screen import Candidate, Constraints, ScreenResult, screen

D = Decimal
W = MfScreenWeights()
NONE = Constraints()


def lines(*pairs: tuple[str, str]) -> list[FundHoldingRow]:
    return [
        FundHoldingRow(month_end=date(2025, 12, 31), isin=i, weight_pct=D(w), source="mf_holdings")
        for i, w in pairs
    ]


def cand(code: str, **kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "amfi_code": code, "name": f"Fund {code}", "plan": "direct", "option": "growth",
        "aum_crore": D("500"), "ter_pct": D("0.50"), "tenure_years": D("5"),
        "beat_pct": D("60"), "median_excess_pct": D("1"), "downside_capture_pct": D("90"),
        "valuation_ratio": D("1.0"),
    }  # fmt: skip
    base.update(kw)
    return Candidate(**base)


def codes(r: ScreenResult) -> list[str]:
    return [x.amfi_code for x in r.ranked]


def test_filters_direct_plan_min_aum_max_ter_min_tenure() -> None:
    cs = [
        cand("1"), cand("2", plan="regular"), cand("3", aum_crore=D("10")),
        cand("4", ter_pct=D("2")), cand("5", tenure_years=D("1")),
    ]  # fmt: skip
    k = Constraints(min_aum_crore=D("100"), max_ter=D("1"), min_tenure_years=D("3"))
    r = screen(cs, k, {}, W)
    assert codes(r) == ["1"]
    got = {x.amfi_code: x.reasons for x in r.rejected}
    assert got["2"] == ["not a direct plan"]
    assert got["3"] == ["AUM (crore) 10 fails the requested limit 100"]
    assert got["4"] == ["TER 2 fails the requested limit 1"]
    assert got["5"] == ["manager tenure 1 fails the requested limit 3"]


def test_unknown_aum_ter_or_tenure_excluded_with_reason() -> None:
    cs = [
        cand("1", aum_crore=None),
        cand("2", ter_pct=None),
        cand("3", tenure_years=None),
        cand("4"),
    ]
    k = Constraints(min_aum_crore=D("1"), max_ter=D("5"), min_tenure_years=D("1"))
    r = screen(cs, k, {}, W)
    assert codes(r) == ["4"]
    assert {x.amfi_code: x.reasons for x in r.rejected} == {
        "1": ["AUM (crore) unknown"], "2": ["TER unknown"], "3": ["manager tenure unknown"],
    }  # fmt: skip
    # a constraint nobody asked for does not exclude an unknown value
    assert codes(screen([cand("1", aum_crore=None)], NONE, {}, W)) == ["1"]


def test_idcw_and_non_comparable_excluded() -> None:
    cs = [cand("1", option="idcw"), cand("2", comparable=False), cand("3", plan=None), cand("4")]
    r = screen(cs, NONE, {}, W)
    assert codes(r) == ["4"]
    got = {x.amfi_code: x.reasons for x in r.rejected}
    assert got["1"] == ["IDCW option (NAV is not comparable)"]
    assert got["2"] == ["returns not comparable"] and got["3"] == ["plan unknown"]


def test_returns_at_most_five_ranked() -> None:
    cs = [cand(str(100 + i), ter_pct=D("0.1") * (i + 1)) for i in range(8)]
    r = screen(cs, NONE, {}, W)
    assert len(r.ranked) == 5 and codes(r) == ["100", "101", "102", "103", "104"]


def test_ranking_uses_consistency_downside_cost_valuation_known_answer() -> None:
    a = cand(
        "1",
        beat_pct=D("80"),
        downside_capture_pct=D("95"),
        ter_pct=D("1.0"),
        valuation_ratio=D("1.2"),
    )
    b = cand(
        "2",
        beat_pct=D("70"),
        downside_capture_pct=D("80"),
        ter_pct=D("0.5"),
        valuation_ratio=D("1.0"),
    )
    c = cand(
        "3",
        beat_pct=D("60"),
        downside_capture_pct=D("90"),
        ter_pct=D("0.8"),
        valuation_ratio=D("0.9"),
    )
    r = screen([a, b, c], NONE, {}, W)
    # ranks (consistency, downside, cost, valuation): a 1,3,3,3 = 10; b 2,1,1,2 = 6; c 3,2,2,1 = 8
    assert [(x.amfi_code, x.score) for x in r.ranked] == [
        ("2", D("6.0000")), ("3", D("8.0000")), ("1", D("10.0000")),
    ]  # fmt: skip
    assert r.ranked[0].ranks == {"consistency": 2, "downside": 1, "cost": 1, "valuation": 2}


def test_weights_from_config_change_the_order() -> None:
    a = cand(
        "1",
        beat_pct=D("80"),
        downside_capture_pct=D("95"),
        ter_pct=D("1.0"),
        valuation_ratio=D("1.2"),
    )
    b = cand(
        "2",
        beat_pct=D("70"),
        downside_capture_pct=D("80"),
        ter_pct=D("0.5"),
        valuation_ratio=D("1.0"),
    )
    consistency_only = MfScreenWeights(
        consistency=D("1"), downside=D("0"), cost=D("0"), valuation=D("0")
    )
    assert codes(screen([a, b], NONE, {}, consistency_only)) == ["1", "2"]
    cost_only = MfScreenWeights(consistency=D("0"), downside=D("0"), cost=D("1"), valuation=D("0"))
    assert codes(screen([a, b], NONE, {}, cost_only)) == ["2", "1"]


def test_unavailable_valuation_ranks_last_and_is_flagged() -> None:
    a = cand("1", valuation_ratio=None)
    b = cand("2", valuation_ratio=D("1.5"))
    r = screen([a, b], NONE, {}, W)
    by = {x.amfi_code: x for x in r.ranked}
    assert by["2"].ranks["valuation"] == 1 and by["1"].ranks["valuation"] == 2
    assert by["1"].unavailable == ["valuation"] and by["2"].unavailable == []
    nothing = screen(
        [cand("1", beat_pct=None, ter_pct=None, downside_capture_pct=None)], NONE, {}, W
    )
    assert nothing.ranked[0].unavailable == ["consistency", "cost", "downside"]


def test_tie_break_by_amfi_code_deterministic() -> None:
    cs = [cand("30"), cand("10"), cand("20")]
    assert codes(screen(cs, NONE, {}, W)) == ["10", "20", "30"]
    assert codes(screen(cs[::-1], NONE, {}, W)) == ["10", "20", "30"]
    assert {x.ranks["cost"] for x in screen(cs, NONE, {}, W).ranked} == {1}  # ties share a rank


def test_overlap_vs_owned_reports_max_pairwise_and_with_which_fund() -> None:
    c = cand("1", holdings=lines(("INE000A01010", "30"), ("INE111A01011", "20")))
    owned = {
        "900": lines(("INE000A01010", "10")),
        "901": lines(("INE000A01010", "30"), ("INE111A01011", "5")),
    }
    got = screen([c], NONE, owned, W).ranked[0].overlap
    assert got is not None and (got.overlap_pct, got.with_fund) == (D("35.00"), "901")
    assert screen([c], NONE, {}, W).ranked[0].overlap is None


def test_already_owned_funds_are_not_candidates() -> None:
    r = screen([cand("900"), cand("1")], NONE, {"900": lines(("INE000A01010", "10"))}, W)
    assert codes(r) == ["1"] and r.rejected[0].reasons == ["already owned"]


def test_category_with_no_candidates_returns_empty_with_reason() -> None:
    r = screen([], NONE, {}, W)
    assert r.ranked == [] and r.reason == "no candidates in the category"
    every = screen([cand("1", plan="regular")], NONE, {}, W)
    assert every.ranked == [] and every.reason == "every candidate was rejected"


def test_screen_deterministic_byte_identical() -> None:
    cs = [cand("2", ter_pct=D("0.3")), cand("1"), cand("3", valuation_ratio=None)]
    a = json.dumps(dataclasses.asdict(screen(cs, NONE, {}, W)), default=str, sort_keys=True)
    b = json.dumps(dataclasses.asdict(screen(cs[::-1], NONE, {}, W)), default=str, sort_keys=True)
    assert a == b


def test_overlap_unknown_without_candidate_holdings_not_zero() -> None:
    owned = {"900": lines(("INE000A01010", "10"))}
    assert screen([cand("1")], NONE, owned, W).ranked[0].overlap is None
