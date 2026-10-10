from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0008-analysis-engines.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "Wilder",
        "TA-Lib",
        "seeded",
        "Decimal",
        "BR-12",
        "weights_version",
        "weights_digest",
        "price-derived",
        "unverified",
        "owner-set",
        "no new dependency",
        "bracketed",
        "not_evaluable",
        "explicit universe",
        "nivesh-engine",
    ):
        assert needle in t, needle


def test_adr_records_the_semantics_the_implementation_chose() -> None:
    t = text()
    for needle in (
        "not Newton or Brent",
        "over-covered",
        "exact coverage",
        "base_breakout",
        "pullback_to_50dma",
        "India limits",
        "never presented as EBIT or EBITDA",
        "cohort of one scores 50",
        'cap = "HOLD"',
    ):
        assert needle in t, needle


def test_adr_lists_each_deferral_by_number() -> None:
    t = text()
    for n in range(1, 14):
        assert f"D{n}:" in t, n


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
