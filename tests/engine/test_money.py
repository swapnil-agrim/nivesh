import re
from datetime import date
from decimal import Decimal

from nivesh_engine.money import CRORE, LAKH, inr_text, usd_text, usd_with_inr

D = Decimal


def test_inr_below_a_lakh_uses_plain_grouping() -> None:
    assert inr_text(D("0")) == "₹0.00"
    assert inr_text(D("999")) == "₹999.00"
    assert inr_text(D("1234.5")) == "₹1,234.50"
    assert inr_text(D("12345.678")) == "₹12,345.68"


def test_exactly_one_lakh_is_1_00_lakh() -> None:
    assert inr_text(D(LAKH)) == "₹1.00 lakh"
    assert inr_text(D("2550000")) == "₹25.50 lakh"


def test_99999_99_stays_plain_and_100000_flips_to_lakh() -> None:
    assert inr_text(D("99999.99")) == "₹99,999.99"
    assert inr_text(D("99999.996")) == "₹1.00 lakh"  # rounds up across the boundary
    assert inr_text(D("100000")) == "₹1.00 lakh"


def test_exactly_one_crore_is_1_00_crore() -> None:
    assert inr_text(D(CRORE)) == "₹1.00 crore"
    assert inr_text(D("9999999")) == "₹1.00 crore"  # 99.99999 lakh rounds across to a crore
    assert inr_text(D("9990000")) == "₹99.90 lakh"


def test_crore_above_a_thousand_uses_indian_grouping_never_lakh_crore() -> None:
    assert inr_text(D("1920000000000")) == "₹1,92,000.00 crore"
    assert inr_text(D("12345678900000")) == "₹12,34,567.89 crore"
    assert "lakh crore" not in inr_text(D("10") ** 15)


def test_negative_amounts_keep_sign() -> None:
    assert inr_text(D("-1234.5")) == "-₹1,234.50"
    assert inr_text(D("-250000")) == "-₹2.50 lakh"
    assert inr_text(D("-20000000")) == "-₹2.00 crore"


def test_usd_two_decimals_with_thousands_grouping() -> None:
    assert usd_text(D("5")) == "$5.00"
    assert usd_text(D("1234567.891")) == "$1,234,567.89"
    assert usd_text(D("-1234.5")) == "-$1,234.50"


def test_usd_with_inr_states_rate_and_rate_date() -> None:
    out = usd_with_inr(D("100"), D("85"), date(2026, 1, 2))
    assert out == "$100.00 (₹8,500.00 at 85.00 INR/USD on 2026-01-02)"


def test_results_are_exact_decimals_half_up_not_binary() -> None:
    assert inr_text(D("0.005")) == "₹0.01"  # half-even would give 0.00
    assert inr_text(D("1.125")) == "₹1.13"
    assert usd_text(D("2.675")) == "$2.68"


def test_output_never_contains_a_nine_digit_run() -> None:
    for v in ("123456789", "123456789012345678", "-99999999999", "1234567890123"):
        for text in (inr_text(D(v)), usd_text(D(v)), usd_with_inr(D(v), D("85"), date(2026, 1, 2))):
            assert not re.search(r"\d{9}", text), text
