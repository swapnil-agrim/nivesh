from datetime import date
from decimal import Decimal

from nivesh_engine.citations import ALL, Evidence, Figure, figures, matches, validate

D = Decimal


def vals(text: str) -> list[tuple[str, Decimal | None, int, str]]:
    return [(t.text, t.value, t.decimals, t.unit) for t in figures(text)]


def test_parses_western_and_indian_grouping() -> None:
    assert [t.value for t in figures("12,345 and 1,23,456.70 and 1,234,567")] == [
        D(12345), D("123456.70"), D(1234567),
    ]  # fmt: skip
    assert [t.decimals for t in figures("1,23,456.70 and 3")] == [2, 0]


def test_parses_decimals_signs_and_percent() -> None:
    assert vals("fell -3.25% then +4.5%") == [
        ("-3.25%", D("-3.25"), 2, "%"), ("+4.5%", D("4.5"), 1, "%"),
    ]  # fmt: skip
    assert figures("down −2.5")[0].value == D("-2.5")
    assert [t.value for t in figures("range 100-110")] == [D(100), D(110)]  # a dash is not a sign


def test_parses_currency_prefixes_and_lakh_crore_k_m_bn_suffixes() -> None:
    got = {t.text: (t.value, t.unit, t.scale) for t in figures(
        "₹1.20 crore $5m 3 lakh 12k 2.5bn 4 cr 1.5x")}  # fmt: skip
    assert got["₹1.20 crore"] == (D("1.20"), "crore", D(10) ** 7)
    assert got["$5m"] == (D(5), "m", D(10) ** 6)
    assert got["3 lakh"][2] == D(10) ** 5 and got["12k"][2] == D(1000)
    assert got["2.5bn"][2] == D(10) ** 9 and got["4 cr"][1] == "cr"
    assert got["1.5x"] == (D("1.5"), "x", D(1))


def test_records_decimals_shown_and_scale() -> None:
    (t,) = figures("price 103.5500")
    assert (t.decimals, t.scale, t.unit) == (4, D(1), "")
    assert figures("5 km")[0].unit == ""  # `k` followed by a letter is not a unit


def test_skips_figures_glued_to_letters() -> None:
    assert figures("SMA200 Q3FY26 3G H1 file2") == []
    assert [t.text for t in figures("above SMA200 by 4.5%")] == ["4.5%"]


def test_skips_line_start_ordinals() -> None:
    text = "1. First point 5\n2) second 7.5\n- 3. third\nplain 4"
    assert [t.text for t in figures(text)] == ["5", "7.5", "4"]
    assert [t.text for t in figures("1.20 crore at start")] == ["1.20 crore"]


def test_iso_and_day_month_year_dates_parse_as_date_figures() -> None:
    got = figures("on 2026-01-02 and 5 Mar 2025 then 12")
    assert [(t.text, t.day) for t in got if t.is_date] == [
        ("2026-01-02", date(2026, 1, 2)), ("5 Mar 2025", date(2025, 3, 5)),
    ]  # fmt: skip
    assert [t.text for t in got if not t.is_date] == ["12"]  # the date digits are not numbers
    assert figures("2026-13-45") != [] and not any(t.is_date for t in figures("2026-13-45"))


def test_no_float_in_results() -> None:
    for t in figures("1.5 2,00,000.25 3%"):
        assert (
            isinstance(t, Figure) and isinstance(t.value, Decimal) and isinstance(t.scale, Decimal)
        )


# ---- matching ------------------------------------------------------------------------------
def ev(*nums: str, scope: str = ALL) -> Evidence:
    e = Evidence()
    for n in nums:
        e.add_number(D(n), scope)
    return e


def ok(text: str, e: Evidence, scope: str = ALL) -> bool:
    return all(matches(t, e, scope) for t in figures(text))


def test_match_within_half_unit_of_last_displayed_digit() -> None:
    e = ev("103.55817388")
    assert ok("103.56", e) and ok("103.6", e) and ok("104", e)
    assert not ok("103.57", e) and not ok("103.5", e) and not ok("105", e)


def test_rounding_boundary_12_345_vs_12_35_both_modes_covered() -> None:
    e = ev("12.345")
    assert ok("12.35", e) and ok("12.34", e)  # half-up and half-even readings both pass
    assert not ok("12.36", e) and not ok("12.33", e)


def test_percent_vs_fraction_scale() -> None:
    assert ok("12.5%", ev("12.5")) and ok("12.5%", ev("0.125"))
    assert not ok("12.5%", ev("0.25")) and not ok("12.5%", ev("1.25"))
    assert not ok("0.125", ev("12.5"))  # a bare fraction is not read as percent


def test_lakh_crore_scale_matches_raw_rupees() -> None:
    assert ok("₹1.20 crore", ev("12000000")) and ok("₹1.20 crore", ev("1.2"))
    assert ok("₹1.20 crore", ev("120"))  # 120 lakh is the same amount
    assert not ok("₹1.20 crore", ev("12"))
    assert ok("2.50 lakh", ev("250000")) and ok("2.50 lakh", ev("0.025"))  # 0.025 crore
    assert ok("1.20 crore", ev("12000000.4"))  # within half of 0.01 crore
    assert not ok("1.20 crore", ev("13000000"))


def test_integers_match_exactly() -> None:
    assert ok("18", ev("18")) and ok("18", ev("18.4")) and not ok("19", ev("18"))
    assert not ok("2026", ev("2025")) and ok("2026", ev("2026"))


def test_unregistered_derived_value_is_not_inferred() -> None:
    e = ev("100", "110")  # a 10% rise exists only as a derived figure
    assert not ok("10%", e)
    e.add_registered(D("10"), 0, "%")
    assert ok("10%", e)


def test_registered_num_always_matches() -> None:
    table = [(D("62.5"), 1, ""), (D("1.20"), 2, "crore"), (D("-2.50"), 2, "lakh"), (D("7"), 0, "%")]
    e = Evidence()
    for v, d, u in table:
        e.add_registered(v, d, u)
    assert ok("62.5", e) and ok("₹1.20 crore", e) and ok("-₹2.50 lakh", e) and ok("2.50 lakh", e)
    assert ok("7%", e) and not ok("8%", e)


def test_empty_evidence_flags_everything() -> None:
    v = validate([("b1", ALL, "price 10.5 and 3 items")], Evidence())
    assert v.checked == 2 and [m.figure for m in v.unmatched] == ["10.5", "3"]
    assert not v.clean and v.unmatched[0].block_id == "b1" and "10.5" in v.unmatched[0].context


def test_invented_number_flagged_structural_numbers_not() -> None:
    e = ev("101.5", "62")
    v = validate(
        [("a", ALL, "1. Close was 101.5 with a score of 62\n2. Target 150.25 is likely")], e
    )
    assert [m.figure for m in v.unmatched] == ["150.25"] and v.checked == 3
    assert validate([("a", ALL, "SMA200 and Q3FY26\n1. item")], Evidence()).clean


def test_redacted_neighbour_means_cannot_verify_not_verified() -> None:
    e = Evidence()
    e.add_text("balance [REDACTED]5.5 and 7.25")
    assert e.redacted and not ok("5.5", e) and ok("7.25", e)
    e.add_text("close 9.5")
    v = validate([("b", ALL, "got 5.5 and 9.5")], e)
    assert [m.figure for m in v.unmatched] == ["5.5"] and "redacted" in v.unmatched[0].reason


def test_tolerance_is_capped_by_max_digits() -> None:
    e = ev("1.23456789")
    (t,) = figures("1.2345680")  # seven decimals shown; evidence differs at the 7th
    assert matches(t, e, ALL, max_digits=6) and not matches(t, e, ALL, max_digits=9)
    assert not matches(figures("1.2")[0], ev("1.9"))  # the cap only limits tightening


def test_number_from_another_securitys_tool_result_does_not_match() -> None:
    e = Evidence()
    e.add_number(D("55.5"), "7")
    e.add_number(D("10"), ALL)
    assert ok("55.5", e, "7") and not ok("55.5", e, "8") and ok("10", e, "8")
    v = validate([("sec8", "8", "close 55.5")], e)
    assert [m.figure for m in v.unmatched] == ["55.5"]


def test_dates_match_evidence_dates_by_scope() -> None:
    e = Evidence()
    e.add_text("as_of 2026-01-02", "7")
    assert ok("2026-01-02", e, "7") and not ok("2026-01-02", e, "8")
    e.add_day(date(2026, 3, 1))
    assert ok("1 Mar 2026", e, "8")
    assert e.numbers == {}  # the date digits did not become numbers


def test_add_json_walks_nested_values() -> None:
    e = Evidence()
    e.add_json({"a": [1, {"b": D("2.5"), "c": "close 3.75 on 2026-02-03"}], "d": True, "e": None})
    assert ok("1", e) and ok("2.5", e) and ok("3.75", e) and ok("2026-02-03", e)
    assert not ok("5", e)


def test_number_after_a_non_grouping_comma_is_its_own_figure() -> None:
    assert [t.value for t in figures("Revenue 12.5,14.2 grows")] == [D("12.5"), D("14.2")]
    assert [t.value for t in figures("1,2,3")] == [D(1), D(2), D(3)]
    assert [t.value for t in figures("1,23,456.7 and 12,345")] == [D("123456.7"), D(12345)]
    ev = Evidence()
    ev.add_number(D("12.5"))
    assert [m.figure for m in validate([("b", ALL, "Revenue 12.5,987.7 units")], ev).unmatched] == [
        "987.7"
    ]


def test_currency_code_prefix_and_word_units() -> None:
    got = {t.text: (t.value, t.unit, t.scale) for t in figures("USD777.7, INR 5, 1.2 million")}
    assert got["USD777.7"][0] == D("777.7")
    assert got["1.2 million"] == (D("1.2"), "million", D(10) ** 6)
    assert figures("3 billion")[0].scale == D(10) ** 9 and figures("4 thousand")[0].scale == D(1000)
    ev = Evidence()
    ev.add_number(D(1200000))
    assert validate([("b", ALL, "about 1.2 million")], ev).clean
