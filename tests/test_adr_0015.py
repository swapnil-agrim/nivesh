from pathlib import Path

from nivesh_core.pii_scan import scan_text

DOCS = Path(__file__).resolve().parents[1] / "docs" / "adr"
ADR = DOCS / "0015-india-fundamentals-source-tapetide.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_the_evaluated_options_and_the_choice() -> None:
    t = text()
    for needle in (
        "Tapetide", "NSE", "Yahoo", "BSE", "CR-7", "REJECTED", "CHOSEN", "50 calls", "1,000",
        "60 calls", "personal use", "robots.txt", "403", "Akamai",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_records_the_live_evidence_and_the_limits() -> None:
    t = text()
    for needle in (
        "daily", "cache", "reported", "estimated", "minority", "TAPETIDE_TOKEN", "Face Value",
        "INR crore", "13 quarters", "12 fiscal years", "net of excise", "merger", "halt",
        "Retry-After", "x-ratelimit-reset", "redistribution", "trimmed", "SYNTHETIC",
        "financing_profit", "_INDIA_GAPS", "DCF", "P/B history", "overwrite", "split",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_decision_d1_to_d12_and_each_deferral_x1_to_x4() -> None:
    t = text()
    for n in range(1, 13):
        assert f"D{n} " in t, n
    for n in range(1, 5):
        assert f"X{n}:" in t, n
    assert t.startswith("# ADR-0015") and "Status: accepted." in t


def test_adr_0005_points_to_this_decision() -> None:
    assert "ADR-0015" in (DOCS / "0005-india-fundamentals-source.md").read_text()


def test_adr_has_no_pii_or_credential_literal() -> None:
    assert scan_text(ADR.read_text()) == []
