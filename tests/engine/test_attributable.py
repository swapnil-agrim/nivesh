from decimal import Decimal, localcontext

import pytest

from nivesh_engine import attributable as attr
from nivesh_engine.attributable import (
    CONSOLIDATED,
    EPS_X_SHARES,
    GAP_TOLERANCE_PCT,
    NO_MINORITY,
    REPORTED_MINORITY,
    UNKNOWN,
    Attributable,
    attributable_profit,
)
from tests import tapetide_fx as tx

D = Decimal
CRORE = D(10) ** 7


def derive(
    ni: str | None, eps: str | None = "1", shares: str | None = "10000", mi: str | None = None
) -> Attributable:
    """EPS 1 x 10,000 shares = 10,000, so `ni` is the gap in basis points of 100 percent."""
    return attributable_profit(
        None if ni is None else D(ni),
        minority_interest=None if mi is None else D(mi),
        eps=None if eps is None else D(eps),
        shares_out=None if shares is None else D(shares),
    )


# ---- reported minority interest -----------------------------------------------------------------
def test_a_reported_minority_interest_is_subtracted_and_wins_over_eps_x_shares() -> None:
    got = derive("100", mi="20", eps="0.5", shares="100")  # EPS x shares would say 50
    assert (got.value, got.basis, got.gap_pct) == (D(80), REPORTED_MINORITY, None)
    assert got.consolidated == D(100) and got.reason is None


def test_a_zero_or_loss_making_minority_is_still_a_reported_basis() -> None:
    zero = derive("100", mi="0")
    assert zero.value == D(100) and zero.basis == REPORTED_MINORITY
    assert derive("100", mi="-5").value == D(105)  # the minority's share of a loss adds back


# ---- EPS x shares and the 3 percent rule --------------------------------------------------------
def test_the_tolerance_is_a_module_constant_of_three_percent() -> None:
    assert GAP_TOLERANCE_PCT == 3 and isinstance(GAP_TOLERANCE_PCT, Decimal)


def test_a_gap_above_the_tolerance_uses_eps_x_shares() -> None:
    got = derive("12500", eps="1", shares="10000")  # +25.00 percent
    assert (got.value, got.basis, got.gap_pct) == (D(10000), EPS_X_SHARES, D("25.00"))
    assert got.consolidated == D(12500) and got.reason is None


@pytest.mark.parametrize(
    ("ni", "basis", "value"),
    [
        ("10300", NO_MINORITY, "10300"),  # +3.00: inside, inclusive
        ("10301", EPS_X_SHARES, "10000"),  # +3.01: outside
        ("9700", NO_MINORITY, "9700"),  # -3.00: inside, inclusive
        ("9699", UNKNOWN, "9699"),  # -3.01: outside, minority interest cannot explain it
        ("10000", NO_MINORITY, "10000"),
    ],
)
def test_the_tolerance_edges_are_inclusive_and_symmetric(ni: str, basis: str, value: str) -> None:
    got = derive(ni)
    assert got.basis == basis and got.value == D(value) and got.consolidated == D(ni)


def test_a_gap_within_the_tolerance_records_the_gap_and_keeps_net_income() -> None:
    got = derive("10160")  # +1.60
    assert (got.value, got.basis, got.gap_pct, got.reason) == (
        D(10160), NO_MINORITY, D("1.60"), None,
    )  # fmt: skip


def test_the_gap_is_judged_on_the_two_digit_figure_that_is_reported() -> None:
    assert derive("10300.4").gap_pct == D("3.00") and derive("10300.4").basis == NO_MINORITY
    assert derive("10300.6").gap_pct == D("3.01") and derive("10300.6").basis == EPS_X_SHARES


@pytest.mark.parametrize("ni", ["9500", "5000", "-100"])
def test_a_gap_below_the_tolerance_falls_back_to_the_consolidated_figure_with_a_reason(
    ni: str,
) -> None:
    got = derive(ni)
    gap = (D(ni) - D(10000)) / D(10000) * 100
    assert got.value == D(ni) and got.basis == UNKNOWN and got.consolidated == D(ni)
    assert got.gap_pct == gap.quantize(D("0.01"))
    reason = got.reason or ""
    assert f"{gap:+.2f}%" in reason  # names the gap with its sign
    for why in ("weighted EPS", "period-end shares", "buybacks", "data mismatch"):
        assert why in reason
    assert "not available" not in reason


def test_the_reason_is_sign_neutral_the_retired_note_said_above_for_a_negative_gap() -> None:
    for ni in ("9000", "9699", "11000", "12500"):
        got = derive(ni)
        text = " ".join(str(v) for v in got.inputs().values()) + (got.reason or "")
        assert "above EPS" not in text and "below EPS" not in text


# ---- unavailable inputs keep the number, labelled -----------------------------------------------
@pytest.mark.parametrize(
    ("eps", "shares", "missing"),
    [
        (None, "10000", "eps"),
        ("1", None, "shares_out"),
        (None, None, "eps and shares_out"),
        ("0", "10000", "positive EPS x shares"),
        ("-1", "10000", "positive EPS x shares"),
        ("1", "0", "positive EPS x shares"),
    ],
)
def test_missing_or_non_positive_eps_x_shares_keeps_the_consolidated_figure_labelled(
    eps: str | None, shares: str | None, missing: str
) -> None:
    got = derive("500", eps=eps, shares=shares)
    assert (got.value, got.basis, got.gap_pct, got.consolidated) == (D(500), UNKNOWN, None, D(500))
    assert got.reason == f"minority interest not available: no {missing} for the period"


def test_no_consolidated_net_income_is_the_only_case_without_a_value() -> None:
    got = derive(None, mi="5")
    assert got.value is None and got.consolidated is None and got.basis == UNKNOWN
    assert got.reason and "net income" in got.reason
    assert derive(None).value is None


# ---- how the result is shown --------------------------------------------------------------------
def test_inputs_carry_the_figure_used_both_bases_the_label_the_gap_and_the_reason() -> None:
    got = derive("12500")
    assert got.inputs() == {
        "net_income": D(10000), "net_income_consolidated": D(12500),
        "profit_basis": EPS_X_SHARES, "profit_gap_pct": D("25.00"),
    }  # fmt: skip
    unknown = derive("500", eps=None)
    assert unknown.inputs()["profit_reason"] == unknown.reason
    assert unknown.inputs()["profit_basis"] == UNKNOWN and unknown.inputs()["net_income"] == D(500)
    assert "profit_reason" not in derive("100", mi="1").inputs()


def test_labels_are_the_declared_strings() -> None:
    assert (REPORTED_MINORITY, EPS_X_SHARES) == ("reported_minority", "eps_x_shares")
    assert NO_MINORITY == "consolidated_no_minority_detected"
    assert UNKNOWN == "consolidated_minority_unknown" and CONSOLIDATED == "consolidated"
    assert attr.__doc__ and "ADR-0016" in attr.__doc__


def test_the_result_does_not_depend_on_the_callers_decimal_context() -> None:
    want = derive("12345.6789", eps="3.14159", shares="3000")
    with localcontext() as ctx:
        ctx.prec = 4
        assert derive("12345.6789", eps="3.14159", shares="3000") == want


# ---- the five recorded companies (latest fiscal year, absolute INR) -----------------------------
def latest(company: str) -> tuple[Decimal, Decimal, Decimal]:
    rows = tx.recorded_rows(company)
    fy = max(r.period_end for r in rows if r.item == "shares_out")
    at = {r.item: r.value for r in rows if r.period_end == fy and r.period_type == "A"}
    return at["net_income"], at["eps"], at["shares_out"]


@pytest.mark.parametrize(
    ("company", "gap", "basis", "crore"),
    [
        ("RELIANCE", "18.55", EPS_X_SHARES, "80772.508"),
        ("HDFCBANK", "4.22", EPS_X_SHARES, "76011.21"),
        ("ITC", "1.60", NO_MINORITY, "21018"),
        ("TCS", "0.44", NO_MINORITY, "49454"),
        ("INFY", "0.30", NO_MINORITY, "29474"),
    ],
)
def test_recorded_companies_give_the_gaps_the_plan_verified(
    company: str, gap: str, basis: str, crore: str
) -> None:
    ni, eps, shares = latest(company)
    got = attributable_profit(ni, eps=eps, shares_out=shares)
    assert got.gap_pct == D(gap) and got.basis == basis
    assert got.value is not None and got.value / CRORE == D(crore)
    assert got.consolidated == ni and got.reason is None
