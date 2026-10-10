from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0005-india-fundamentals-source.md"


def text() -> str:
    return ADR.read_text()


def test_adr_compares_four_candidates_on_five_criteria() -> None:
    t = text()
    for cand in ("Tapetide MCP", "Screener-style", "BSE XBRL", "NSE", "Paid trial"):
        assert cand in t, cand
    header = next(line for line in t.splitlines() if line.startswith("| Candidate"))
    for col in ("Coverage", "History depth", "Restatement handling", "Accuracy method", "Licence"):
        assert col in header, col


def test_adr_records_choice_and_fallback() -> None:
    t = text()
    assert "**Primary: exchange XBRL" in t and "**Fallback: Tapetide MCP**" in t


def test_adr_states_provisional_and_links_deferred_sampling() -> None:
    t = text()
    first = t.split("\n\n")[1]
    assert "provisional" in first
    assert "30-company" in t and "10-company manual check" in t
    assert "E4/ST-4.1: run the 30-company India fundamentals sampling" in " ".join(t.split())


def test_adr_closes_q2() -> None:
    assert "Q-2 is closed" in text()


def test_adr_lists_no_new_dependencies_with_justification() -> None:
    t = text()
    for dep in ("yfinance", "feedparser", "exchange_calendars", "rapidfuzz"):
        assert f"`{dep}`" in t, dep


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(text()) == []
