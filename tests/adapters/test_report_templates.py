import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.report import Report, to_html, to_markdown
from nivesh_adapters.report_templates import (
    BRIEF_WORDS,
    brief_report,
    fund_doctor,
    ideas_report,
    portfolio_review,
    prose_words,
    research_note,
    scorecard_report,
)
from tests.report_fx import (
    RUN_AT,
    brief_input,
    doctor,
    ideas_input,
    portfolio_input,
    research_input,
    scorecard_input,
    security_result,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "reports"
D = Decimal
BUILDERS = {
    "research_note": lambda: research_note(research_input()),
    "portfolio_review": lambda: portfolio_review(portfolio_input()),
    "fund_doctor": lambda: fund_doctor(doctor(), run_at=RUN_AT, as_of=date(2026, 1, 2), run_id=7),
    "ideas": lambda: ideas_report(ideas_input()),
    "brief": lambda: brief_report(brief_input()),
    "scorecard": lambda: scorecard_report(scorecard_input()),
}


def heads(r: Report) -> list[str]:
    return [b.heading for b in r.blocks]


def test_research_note_sections_in_pid_order() -> None:
    assert heads(research_note(research_input())) == [
        "Verdict box", "Investment thesis", "Fundamentals", "Valuation", "Technical setup",
        "News and catalysts", "Bull vs bear", "Risks and invalidation", "Sizing",
    ]  # fmt: skip


def test_verdict_box_comes_from_committee_verdict_fields_unchanged() -> None:
    r = research_note(research_input())
    v = security_result().verdict
    (row,) = r.decision.rows
    assert row[1] == v.verdict == "HOLD" and row[2] == v.conviction
    assert row[4] == "100 to 110 INR" and row[5] == v.review_date.isoformat()
    box = next(b for b in r.blocks if b.id == "verdict").text
    assert f"horizon {v.horizon}" in box and "coverage 100%" in box
    assert "Composite score 62.5 (upper)" in box and "Last close ₹105.25, 2026-01-02" in box


def test_missing_section_data_becomes_a_data_gap_line_not_a_blank() -> None:
    r = research_note(research_input(result=security_result(views=("fundamental",)), valuation=()))
    by_id = {b.id: b for b in r.blocks}
    assert by_id["technical"].text.startswith("Data gap: no technical view")
    assert by_id["news"].text.startswith("Data gap: no news view")
    assert by_id["valuation"].text == "No data."
    assert "no valuation range supplied" in r.gaps and "no news view in this run" in r.gaps
    assert all(b.text.strip() for b in r.blocks)


def test_model_text_blocks_are_flagged_checked_and_labels_are_not() -> None:
    r = research_note(research_input())
    checked = {b.id: b.checked for b in r.blocks}
    assert checked["verdict"] is False
    assert checked["cases"] and checked["thesis"] and checked["technical"] and checked["risks"]
    assert "Bull: b" in next(b for b in r.blocks if b.id == "cases").text


def test_usd_security_shows_inr_equivalent_with_rate_date() -> None:
    r = research_note(
        research_input(currency="USD", close=D("100"), usd_inr=(D("85"), date(2026, 1, 1)))
    )
    box = next(b for b in r.blocks if b.id == "verdict").text
    assert "$100.00 (₹8,500.00 at 85.00 INR/USD on 2026-01-01), 2026-01-02" in box
    assert {n.text for n in r.nums} >= {"₹8,500.00", "85.00"} and date(2026, 1, 1) in r.dates


def test_portfolio_review_sections() -> None:
    r = portfolio_review(portfolio_input())
    assert heads(r) == [
        "Health summary", "Allocation vs target", "Concentration and look-through",
        "Fund doctor summary", "Rebalance moves", "Tax notes",
    ]  # fmt: skip
    assert r.decision.headers == ("Holding", "Action", "Weight", "Reason")
    assert "₹2.50 crore" in r.summary[0] and "Data gaps" not in heads(r)
    bare = portfolio_review(portfolio_input(allocation=(), concentration=(), holdings=()))
    assert {"no allocation data", "no concentration data", "no holding actions data"} <= set(
        bare.gaps
    )


def test_actions_table_uses_only_the_five_labels() -> None:
    r = portfolio_review(portfolio_input())
    assert {row[1] for row in r.decision.rows} <= {"HOLD", "ADD", "TRIM", "EXIT", "REVIEW"}
    bad = portfolio_input(
        holdings=(portfolio_input().holdings[0].__class__("X", "KEEP", None, ""),)
    )
    with pytest.raises(ValueError, match="unknown holding action"):
        portfolio_review(bad)


def test_fund_doctor_from_doctorreport_fixture_renders_facts_and_verdict() -> None:
    r = BUILDERS["fund_doctor"]()
    assert r.decision.rows == (
        ("Example Fund", "SWITCH_TO_DIRECT", "TER_GAP_WITH_DIRECT_TWIN (ter_gap_pct)"),
    )
    text = r.blocks[0].text
    assert (
        "Cost gap to a direct plan 0.90%" in text and "sortino unavailable: short history" in text
    )
    assert "Example Skipped Fund: no NAV history" in r.gaps
    empty = fund_doctor(type(doctor())([], []), run_at=RUN_AT, as_of=date(2026, 1, 2))
    assert "no mutual fund holdings reviewed" in empty.gaps


def test_tax_notes_say_facts_not_advice() -> None:
    tax = next(b for b in portfolio_review(portfolio_input()).blocks if b.id == "tax")
    assert tax.text.startswith("- Facts and arithmetic from your own records, not advice.")


def test_ideas_report_lists_every_idea_field() -> None:
    r = ideas_report(ideas_input())
    text = r.blocks[0].text
    for part in ("Thesis: b", "Entry zone: 100 to 110 INR", "Suggested weight: none",
                 "Invalidation: Margins fall two quarters in a row",
                 "What would prove this wrong: r", "Review by 2026-04-02"):  # fmt: skip
        assert part in text
    assert r.decision.rows[0][:3] == ("1", "EXEN", "HOLD")


def test_ideas_report_says_k_of_n_qualified_without_padding() -> None:
    r = ideas_report(ideas_input())
    assert r.summary[0] == "1 of 3 asked-for ideas qualified" and len(r.blocks) == 1
    none = ideas_report(ideas_input(entries=(), qualified=0, message="No idea qualified."))
    assert none.decision.rows == () and none.blocks[0].text == "No idea qualified."
    assert none.summary[0] == "0 of 3 asked-for ideas qualified"


def test_brief_has_one_table_and_at_most_400_words() -> None:
    r = brief_report(brief_input())
    md = to_markdown(r)
    assert len(re.findall(r"(?m)^\| --- ", md)) == 1  # exactly one table
    assert prose_words(md) <= BRIEF_WORDS
    long = brief_input(
        sections=(("Everything", tuple(f"line {i} " + "word " * 20 for i in range(60))),)
    )
    big = brief_report(long)
    assert (
        prose_words(to_markdown(big)) <= BRIEF_WORDS
        and "brief trimmed to fit 400 words" in big.gaps
    )


def test_brief_missing_inputs_print_unavailable_with_reason() -> None:
    r = brief_report(brief_input())
    assert "Sector leaders and laggards: unavailable (sector index mapping not built)" in r.gaps
    assert "unavailable (sector index mapping not built)" in to_markdown(r)


def test_scorecard_empty_state_says_not_enough_data() -> None:
    r = scorecard_report(scorecard_input())
    assert r.blocks[0].text.startswith("Not enough data yet") and "no scored calls" in r.gaps
    full = scorecard_report(
        scorecard_input(rows=(type(scorecard_input().rows[0])("long term", 10, 8, D("62.5")),))
    )
    assert full.gaps == () and full.decision.rows[0] == ("long term", "10", "8", "62.5%")


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_template_renders_from_its_fixture_without_error(name: str) -> None:
    r = BUILDERS[name]()
    md, page = to_markdown(r), to_html(r)
    assert md.startswith("# ") and page.startswith("<!DOCTYPE html>")
    assert "Not investment advice" in md and len(r.summary_lines) == 5


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_golden_matches(name: str) -> None:
    r = BUILDERS[name]()
    assert to_markdown(r) == (FIX / f"{name}.md").read_text()
    assert to_html(r) == (FIX / f"{name}.html").read_text()
