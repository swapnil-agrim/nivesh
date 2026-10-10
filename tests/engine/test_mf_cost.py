from decimal import Decimal

from nivesh_engine.mf_cost import (
    SchemeInfo,
    normalise_scheme,
    option_of,
    plan_of,
    resolve_direct_twin,
    ter_cost,
)

D = Decimal
REG = SchemeInfo("100001", "Example Bluechip Fund - Regular Plan - Growth", "Example Mutual Fund")
DIR = SchemeInfo(
    "100010", "Example Bluechip Fund - Direct Plan - Growth Option", "Example Mutual Fund"
)


def test_normalise_scheme_strips_plan_and_option_tokens() -> None:
    assert (
        normalise_scheme("Example Bluechip Fund - Regular Plan - Growth") == "example bluechip fund"
    )
    assert (
        normalise_scheme("EXAMPLE  Bluechip Fund (Direct) IDCW Payout") == "example bluechip fund"
    )
    assert normalise_scheme("Example & Co Fund Dir Plan Growth") == "example co fund"


def test_plan_and_option_come_from_the_scheme_name() -> None:
    assert plan_of("X Fund - Direct Plan - Growth") == "direct"
    assert plan_of("X Fund Regular Plan Growth") == "regular"
    assert plan_of("X Fund Growth") is None  # unknown is not assumed regular
    assert option_of("X Fund Direct Growth") == "growth"
    assert option_of("X Fund Direct IDCW Reinvestment") == "idcw"
    assert option_of("X Fund Direct Payout of Income Distribution cum capital withdrawal") == "idcw"
    assert option_of("X Fund Direct Bonus") == "other"
    assert REG.plan == "regular" and REG.option == "growth" and DIR.plan == "direct"


def test_regular_resolves_to_direct_twin_amfi_code() -> None:
    other = SchemeInfo("100099", "Another Fund - Direct Plan - Growth", "Example Mutual Fund")
    got = resolve_direct_twin(REG, [REG, other, DIR])
    assert (got.status, got.amfi_code, got.candidates) == ("found", "100010", ["100010"])


def test_ambiguous_twin_returns_candidates_not_a_guess() -> None:
    dup = SchemeInfo("100011", "Example Bluechip Fund - Direct - Growth", "Example Mutual Fund")
    got = resolve_direct_twin(REG, [DIR, dup])
    assert got.status == "ambiguous" and got.amfi_code is None
    assert got.candidates == ["100010", "100011"]
    assert "ambiguous" in (got.reason or "")


def test_no_twin_reports_none_with_reason() -> None:
    got = resolve_direct_twin(REG, [REG])
    assert got.status == "none" and got.amfi_code is None and "no direct plan" in (got.reason or "")
    plain = resolve_direct_twin(DIR, [REG, DIR])
    assert plain.status == "none" and "not a regular plan" in (plain.reason or "")
    unknown = resolve_direct_twin(SchemeInfo("1", "Some Fund Growth"), [DIR])
    assert unknown.status == "none" and "plan is unknown" in (unknown.reason or "")


def test_different_option_or_amc_is_not_a_twin() -> None:
    idcw = SchemeInfo("100012", "Example Bluechip Fund - Direct Plan - IDCW", "Example Mutual Fund")
    other_amc = SchemeInfo("100013", "Example Bluechip Fund - Direct Plan - Growth", "Other House")
    assert resolve_direct_twin(REG, [idcw, other_amc]).status == "none"
    no_amc = SchemeInfo("100014", "Example Bluechip Fund - Direct Plan - Growth")
    assert resolve_direct_twin(REG, [no_amc]).amfi_code == "100014"  # unknown AMC does not block


def test_ter_cost_pct_and_inr_per_year_on_current_value() -> None:
    r = ter_cost(D("1.50"), D("0.60"), D("100000"))
    assert (r.ter_gap_pct, r.inr_per_year, r.available, r.reason) == (
        D("0.90"),
        D("900.00"),
        True,
        None,
    )


def test_ter_cost_unavailable_when_ter_value_or_twin_missing_never_zero() -> None:
    for args, word in (
        ((None, D("0.6"), D("100")), "regular TER"),
        ((D("1.5"), None, D("100")), "direct TER"),
        ((D("1.5"), D("0.6"), None), "current value"),
    ):
        r = ter_cost(*args)
        assert not r.available and r.inr_per_year is None and r.ter_gap_pct is None
        assert word in (r.reason or "")
    twin = resolve_direct_twin(REG, [REG])
    r2 = ter_cost(D("1.5"), None, D("100"), twin=twin)
    assert not r2.available and "direct twin" in (r2.reason or "")


def test_non_positive_gap_reports_no_saving() -> None:
    r = ter_cost(D("0.60"), D("0.60"), D("100000"))
    assert r.ter_gap_pct == D("0.00") and r.inr_per_year is None and not r.available
    assert "no saving" in (r.reason or "")
    assert ter_cost(D("0.5"), D("0.6"), D("100")).ter_gap_pct == D("-0.1")


def test_cost_exact_decimal_no_float() -> None:
    r = ter_cost(D("1.0001"), D("0.0001"), D("33333.33"))
    assert r.ter_gap_pct == D("1.0000") and r.inr_per_year == D("333.33")
    assert isinstance(r.inr_per_year, Decimal) and isinstance(r.ter_gap_pct, Decimal)
