"""`fundamentals compare` engine: pure Decimal maths over what each provider stored. It names
differences and never averages, merges or picks a value (ADR-0016)."""

from datetime import date
from decimal import Decimal

import pytest

from nivesh_core.market_models import ShareholdingRow
from nivesh_engine.crosscheck import (
    Comparison,
    compare,
    shareholding_figures,
    statement_figures,
)
from nivesh_engine.statements import StatementRow

D = Decimal
ORDER = ("tapetide", "upstox")


def two(
    base: str, other: str, figure: str = "A 2025-03-31 net_income"
) -> dict[str, dict[str, Decimal]]:
    return {"tapetide": {figure: D(base)}, "upstox": {figure: D(other)}}


def run(figures: dict[str, dict[str, Decimal]], tol: str = "1", **kw: bool) -> Comparison:
    return compare(figures, ORDER, D(tol), **kw)


# ---- the percent maths against the priority provider --------------------------------------------
def test_a_figure_18_5_percent_below_the_vendor_is_flagged_with_the_exact_percent() -> None:
    got = run(two("95754", "78039.51"))
    (d,) = got.disagreements
    assert (d.figure, d.base_provider, d.provider) == (
        "A 2025-03-31 net_income",
        "tapetide",
        "upstox",
    )
    assert (d.base, d.value, d.delta, d.pct) == (
        D("95754"),
        D("78039.51"),
        D("-17714.49"),
        D("-18.50"),
    )
    assert (got.compared, got.agree, got.single_provider) == (1, 0, False)


@pytest.mark.parametrize(
    ("other", "agrees"),
    [
        ("100", True),
        ("100.5", True),
        ("101", True),
        ("99", True),
        ("101.01", False),
        ("98.99", False),
    ],
)
def test_the_tolerance_is_inclusive_and_relative_to_the_priority_value(
    other: str, agrees: bool
) -> None:
    got = run(two("100", other), "1")
    assert (got.agree, len(got.disagreements)) == ((1, 0) if agrees else (0, 1))


def test_the_percent_is_shown_to_two_places() -> None:
    (d,) = run(two("3", "4")).disagreements
    assert d.pct == D("33.33") and str(d.pct) == "33.33"  # not 33.3333...


def test_shareholding_disagreements_are_ordered_by_points_not_by_percent() -> None:
    figs = {
        "tapetide": {"2025-03-31 promoter_pct": D("10"), "2025-03-31 public_pct": D("50")},
        "upstox": {"2025-03-31 promoter_pct": D("12"), "2025-03-31 public_pct": D("53")},
    }
    got = run(figs, "0.5", points=True)
    # public moves 3 points (6 percent), promoter 2 points (20 percent): points decide
    assert [d.figure for d in got.disagreements] == [
        "2025-03-31 public_pct",
        "2025-03-31 promoter_pct",
    ]


def test_a_negative_base_uses_its_absolute_value_so_the_sign_means_direction() -> None:
    (d,) = run(two("-100", "-90")).disagreements
    assert (d.delta, d.pct) == (D("10"), D("10.00"))  # less negative = higher
    (d,) = run(two("-100", "-120")).disagreements
    assert d.pct == D("-20.00")


def test_a_zero_base_with_a_nonzero_other_is_flagged_without_a_percent() -> None:
    (d,) = run(two("0", "5")).disagreements
    assert (d.base, d.value, d.delta, d.pct) == (D("0"), D("5"), D("5"), None)
    both = run(two("0", "0"))
    assert (both.compared, both.agree, both.disagreements) == (1, 1, ())


def test_a_huge_ratio_does_not_overflow_the_percent() -> None:
    (d,) = run(two("0.0001", "9" * 24)).disagreements
    assert d.pct is not None and d.pct > D("1e27")


def test_there_is_no_merged_or_averaged_value_anywhere_in_the_result() -> None:
    got = run(two("100", "60"))
    (d,) = got.disagreements
    assert {d.base, d.value} == {D("100"), D("60")}  # the two stored values, untouched
    assert not any(hasattr(got, n) for n in ("value", "merged", "average", "mean", "values"))


def test_disagreements_are_largest_first_then_by_key_and_agreeing_figures_are_only_counted() -> (
    None
):
    figs = {
        "tapetide": {"k1": D("100"), "k2": D("100"), "k3": D("100"), "k4": D("100"), "k5": D("0")},
        "upstox": {"k1": D("100"), "k2": D("90"), "k3": D("50"), "k4": D("110"), "k5": D("1")},
    }
    got = run(figs)
    assert [d.figure for d in got.disagreements] == ["k5", "k3", "k2", "k4"]  # zero base first
    assert [d.pct for d in got.disagreements] == [None, D("-50.00"), D("-10.00"), D("10.00")]
    assert (got.compared, got.agree) == (5, 1)


# ---- who is compared with whom -------------------------------------------------------------------
def test_one_provider_with_figures_is_a_single_provider_with_nothing_to_compare() -> None:
    got = run({"tapetide": {"k": D("1")}, "upstox": {}})
    assert got.providers == ("tapetide",) and got.single_provider is True
    assert (got.compared, got.agree, got.disagreements, dict(got.only_in)) == (0, 0, (), {})
    none = run({})
    assert none.providers == () and none.single_provider is True


def test_providers_are_listed_in_priority_order_not_input_order() -> None:
    figs = {"upstox": {"k": D("1")}, "tapetide": {"k": D("2")}}
    got = run(figs)
    assert got.providers == ("tapetide", "upstox")
    (d,) = got.disagreements
    assert (d.base_provider, d.base, d.provider, d.value) == ("tapetide", D("2"), "upstox", D("1"))


def test_a_provider_missing_from_the_order_is_listed_last_by_name() -> None:
    figs = {"zeta": {"k": D("1")}, "tapetide": {"k": D("2")}, "alpha": {"k": D("3")}}
    assert run(figs).providers == ("tapetide", "alpha", "zeta")


def test_figures_one_provider_has_and_the_other_lacks_are_listed_not_compared() -> None:
    figs = {"tapetide": {"k1": D("1"), "k2": D("2")}, "upstox": {"k2": D("2"), "k3": D("3")}}
    got = run(figs)
    assert dict(got.only_in) == {"tapetide": ("k1",), "upstox": ("k3",)}
    assert (got.compared, got.agree) == (1, 1)


def test_with_three_providers_each_figure_is_compared_against_its_best_holder() -> None:
    figs = {
        "tapetide": {"a": D("100")},
        "upstox": {"a": D("100"), "b": D("200")},
        "bse_xbrl": {"a": D("150"), "b": D("200"), "c": D("1")},
    }
    got = compare(figs, ("tapetide", "upstox", "bse_xbrl"), D(1))
    assert got.providers == ("tapetide", "upstox", "bse_xbrl")
    pairs = {(d.figure, d.base_provider, d.provider) for d in got.disagreements}
    assert pairs == {("a", "tapetide", "bse_xbrl")}  # b: upstox is the base and bse_xbrl agrees
    assert (got.compared, got.agree) == (3, 2)  # a: two pairs, b: one pair
    assert dict(got.only_in) == {"bse_xbrl": ("c",)}


# ---- shareholding: percentage points -------------------------------------------------------------
@pytest.mark.parametrize(("other", "agrees"), [("50.0", True), ("50.5", True), ("49.5", True),
                                              ("50.6", False), ("49.4", False)])  # fmt: skip
def test_shareholding_differs_by_percentage_points_not_by_percent(other: str, agrees: bool) -> None:
    got = run(two("50", other, "2025-03-31 promoter_pct"), "0.5", points=True)
    assert len(got.disagreements) == (0 if agrees else 1)
    if not agrees:
        d = got.disagreements[0]
        assert d.delta == D(other) - D(50)


def test_a_pledge_of_zero_against_one_point_is_a_difference_in_points() -> None:
    (d,) = run(two("0", "1", "2025-03-31 promoter_pledged_pct"), "0.5", points=True).disagreements
    assert (d.delta, d.pct) == (D("1"), None)


# ---- turning stored rows into comparable figures ------------------------------------------------
def srow(
    item: str, value: str, filed: str, ptype: str = "A", end: str = "2025-03-31"
) -> StatementRow:
    return StatementRow(
        period_end=date.fromisoformat(end),
        period_type=ptype,  # type: ignore[arg-type]
        item=item,
        value=D(value),
        currency="INR",
        filed_at=date.fromisoformat(filed),
    )


def test_statement_figures_take_the_latest_filing_known_on_the_date() -> None:
    rows = {
        "tapetide": [
            srow("net_income", "100", "2025-06-01"),
            srow("net_income", "104", "2025-09-01"),
            srow("net_income", "30", "2025-06-01", "Q", "2025-03-31"),
        ]
    }
    now = statement_figures(rows, ("tapetide",), None)
    assert now == {"A 2025-03-31 net_income": D("104"), "Q 2025-03-31 net_income": D("30")}
    early = statement_figures(rows, ("tapetide",), date(2025, 7, 1))
    assert early["A 2025-03-31 net_income"] == D("100")
    assert statement_figures(rows, ("tapetide",), date(2025, 1, 1)) == {}


def test_a_provider_with_two_stored_sources_uses_the_first_source_per_figure() -> None:
    rows = {
        "tapetide_est": [srow("revenue", "9", "2025-06-01"), srow("cfo", "7", "2025-06-01")],
        "tapetide": [srow("revenue", "10", "2025-05-01")],
        "upstox": [srow("revenue", "1", "2025-05-01")],  # another provider: never read here
    }
    got = statement_figures(rows, ("tapetide", "tapetide_est"), None)
    assert got == {"A 2025-03-31 revenue": D("10"), "A 2025-03-31 cfo": D("7")}
    assert statement_figures(rows, ("nobody",), None) == {}


def held(promoter: str | None, public: str | None, pledged: str | None,
         end: str = "2025-03-31") -> ShareholdingRow:  # fmt: skip
    def num(v: str | None) -> Decimal | None:
        return None if v is None else D(v)

    return ShareholdingRow(period_end=date.fromisoformat(end), promoter_pct=num(promoter),
                           public_pct=num(public), promoter_pledged_pct=num(pledged),
                           filed_at=date(2025, 5, 1))  # fmt: skip


def test_shareholding_figures_take_the_whole_quarter_from_one_source() -> None:
    rows = {
        "tapetide": [held("50", None, "2")],
        "tapetide_est": [held("49", "51", "3"), held("48", "52", None, "2024-12-31")],
    }
    got = shareholding_figures(rows, ("tapetide", "tapetide_est"))
    assert got == {  # the quarter the first source has is theirs alone: no public % borrowed
        "2025-03-31 promoter_pct": D("50"),
        "2025-03-31 promoter_pledged_pct": D("2"),
        "2024-12-31 promoter_pct": D("48"),
        "2024-12-31 public_pct": D("52"),
    }
    assert shareholding_figures(rows, ()) == {}
