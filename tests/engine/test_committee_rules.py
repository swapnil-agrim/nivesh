import ast
import itertools
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_engine import committee_rules as cr
from nivesh_engine.committee_rules import (
    LADDER,
    Bounds,
    below_minimum,
    coverage_pct,
    final_weight,
    risk_veto,
    verdict_ceiling,
    weight_bounds,
)
from tests.agents import committee_fx as fx

D = Decimal
SRC = Path(cr.__file__)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"excluded": True}, "excluded_by_profile"),
        ({"hard_flag": True}, "hard_red_flag"),
        ({"position_over_limit": True}, "position_over_limit"),
        ({"sector_over_limit": True}, "sector_over_limit"),
    ],
)
def test_veto_true_for_each_single_cause_exclusion_hard_flag_position_over_limit_sector_over_limit(
    over: dict[str, bool], reason: str
) -> None:
    v = risk_veto(fx.facts(**over))
    assert v.veto is True and v.reasons == (reason,)


def test_veto_true_when_days_to_trade_exceeds_the_configured_maximum_and_off_when_unset() -> None:
    slow = fx.facts(days_to_trade=D("12.5"))
    assert risk_veto(slow).veto is False  # off by default
    assert risk_veto(slow, max_days_to_trade=D(10)).reasons == ("days_to_trade_over_limit",)
    assert risk_veto(slow, max_days_to_trade=D("12.5")).veto is False  # strictly greater
    assert risk_veto(fx.facts(days_to_trade=None), max_days_to_trade=D(1)).veto is False


def test_veto_false_when_no_cause_applies() -> None:
    v = risk_veto(fx.facts())
    assert v.veto is False and v.reasons == ()


def test_limit_comparison_is_strictly_greater_than_exactly_at_limit_is_not_a_breach() -> None:
    # the caller sets the flags with strict comparisons; the rule trusts only the flags
    assert risk_veto(fx.facts(tested_weight_pct=D(10))).veto is False
    assert risk_veto(fx.facts(days_to_trade=D(5)), max_days_to_trade=D(5)).veto is False


def test_weight_bounds_are_min_of_profile_position_sector_headroom_and_proposal() -> None:
    f = fx.facts(max_position_pct=D(10), sector_headroom_pct=D(6))
    assert weight_bounds(f) == Bounds(D(0), D(6))
    assert weight_bounds(f, D(4)) == Bounds(D(0), D(4))
    assert weight_bounds(f, D(8)).max_pct == D(6)
    assert weight_bounds(fx.facts(sector_headroom_pct=None), D(50)).max_pct == D(10)
    assert weight_bounds(fx.facts(sector_headroom_pct=D(50))).max_pct == D(10)


def test_weight_bounds_never_negative_and_zero_headroom_gives_zero() -> None:
    assert weight_bounds(fx.facts(sector_headroom_pct=D(0))).max_pct == D(0)
    assert weight_bounds(fx.facts(sector_headroom_pct=D(-3))).max_pct == D(0)
    assert weight_bounds(fx.facts(), D(-1)).max_pct == D(0)


def test_coverage_is_the_minimum_of_card_inputs_and_required_views() -> None:
    assert coverage_pct(8, 10, 3, 3) == D(80)
    assert coverage_pct(10, 10, 2, 3) == D(200) / 3
    assert coverage_pct(5, 10, 1, 3) == D(100) / 3
    assert coverage_pct(0, 0, 3, 3) == D(0) and coverage_pct(10, 10, 0, 0) == D(0)
    assert coverage_pct(10, 10, 3, 3) == D(100)


def test_coverage_69_fails_and_70_and_100_pass() -> None:
    assert below_minimum(D(69), D(70)) and not below_minimum(D(70), D(70))
    assert not below_minimum(D(100), D(70)) and below_minimum(D("69.99"), D(70))


def oracle(
    proposed: str, *, veto: bool, cap: str | None, hard: bool, band: str, coverage: int
) -> str:
    """The table written out by hand: the expected result for one combination."""
    if band == "insufficient_data" or coverage < 70:
        return "INSUFFICIENT_DATA"
    if proposed in ("BUY", "ACCUMULATE") and (veto or cap == "HOLD" or hard):
        return "HOLD"
    return proposed


CEILINGS = [
    {"veto": False, "cap": None, "hard": False, "band": "top", "coverage": 100},  # none
    {"veto": True, "cap": None, "hard": False, "band": "top", "coverage": 100},
    {"veto": False, "cap": "HOLD", "hard": False, "band": "top", "coverage": 100},
    {"veto": False, "cap": None, "hard": True, "band": "top", "coverage": 100},
    {"veto": False, "cap": None, "hard": False, "band": "insufficient_data", "coverage": 100},
    {"veto": False, "cap": None, "hard": False, "band": "top", "coverage": 69},
    {"veto": False, "cap": None, "hard": False, "band": "top", "coverage": 70},
    {"veto": True, "cap": "HOLD", "hard": True, "band": "insufficient_data", "coverage": 69},
]


def run(proposed: str, c: dict[str, object]) -> cr.Ceiling:
    return verdict_ceiling(
        proposed, veto=bool(c["veto"]), card_band=str(c["band"]), card_cap=c["cap"],  # type: ignore[arg-type]
        hard_flag=bool(c["hard"]), coverage=D(int(c["coverage"])),  # type: ignore[call-overload]
        min_coverage=D(70),
    )  # fmt: skip


def test_verdict_ceiling_table_all_proposed_verdicts_times_every_ceiling_combination() -> None:
    assert len(LADDER) == 7
    for proposed, c in itertools.product(LADDER, CEILINGS):
        got = run(proposed, c)
        want = oracle(
            proposed, veto=bool(c["veto"]), cap=c["cap"], hard=bool(c["hard"]),  # type: ignore[arg-type]
            band=str(c["band"]), coverage=int(c["coverage"]),  # type: ignore[call-overload]
        )  # fmt: skip
        assert got.verdict == want, (proposed, c)
        # never higher on the ladder than proposed, except the forced INSUFFICIENT_DATA
        if got.verdict != "INSUFFICIENT_DATA":
            assert LADDER.index(got.verdict) >= LADDER.index(proposed)
        applies = (
            c["veto"] or c["cap"] == "HOLD" or c["hard"] or c["band"] != "top" or c["coverage"] < 70
        )  # type: ignore[operator]
        if got.verdict in ("BUY", "ACCUMULATE"):
            assert not applies  # upward verdicts only when no ceiling applies
        if c["band"] == "insufficient_data" or c["coverage"] < 70:  # type: ignore[operator]
            assert got.verdict == "INSUFFICIENT_DATA"  # wins over a veto
        assert got.vetoed_by_risk is bool(c["veto"])


def test_ceiling_lists_every_change_in_overrides() -> None:
    kept = run("BUY", CEILINGS[0])
    assert kept.verdict == "BUY" and kept.overrides == ()
    low = run("ACCUMULATE", {**CEILINGS[0], "veto": True, "hard": True})
    assert low.overrides == ("verdict ACCUMULATE -> HOLD: risk veto, hard red flag",)
    forced = run("SELL", CEILINGS[5])
    assert forced.overrides and "INSUFFICIENT_DATA" in forced.overrides[0]
    already = run("INSUFFICIENT_DATA", CEILINGS[5])
    assert already.overrides == ()
    band = run("BUY", CEILINGS[4])
    assert "insufficient data" in band.overrides[0]
    with pytest.raises(ValueError, match="unknown verdict"):
        run("STRONG_BUY", CEILINGS[0])


def test_final_weight_clamps_drops_and_logs() -> None:
    b = Bounds(D(0), D(6))
    assert final_weight("BUY", D(4), b, lowered_by_veto=False) == (D(4), ())
    w, notes = final_weight("ACCUMULATE", D(9), b, lowered_by_veto=False)
    assert w == D(6) and "9 -> 6" in notes[0]
    assert final_weight("BUY", None, b, lowered_by_veto=False) == (None, ())
    for v in ("SELL", "AVOID", "INSUFFICIENT_DATA"):
        assert final_weight(v, D(3), b, lowered_by_veto=False)[0] is None
    assert final_weight("HOLD", D(3), b, lowered_by_veto=True)[0] is None
    assert final_weight("HOLD", D(3), b, lowered_by_veto=False)[0] == D(3)
    assert final_weight("TRIM", D(3), b, lowered_by_veto=False)[0] == D(3)


def test_rules_have_no_sdk_or_mcp_imports() -> None:
    mods = set()
    for node in ast.walk(ast.parse(SRC.read_text())):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert mods <= {"dataclasses", "decimal"}


def test_rules_are_deterministic() -> None:
    f = fx.facts(hard_flag=True, days_to_trade=D(3))
    assert risk_veto(f, max_days_to_trade=D(2)) == risk_veto(f, max_days_to_trade=D(2))
    a = run("BUY", CEILINGS[1])
    assert a == run("BUY", CEILINGS[1])
