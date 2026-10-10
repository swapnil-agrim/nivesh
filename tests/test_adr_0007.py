from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0007-mutual-fund-intelligence.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "nav_point",
        "price index, not TRI",
        "Decimal",
        "unavailable",
        "no new dependency",
        "unverified",
        "aum_crore",
        "BR-12",
        "ambiguous",
        "by_amfi_code",
        "owner-set",
    ):
        assert needle in t, needle


def test_adr_lists_each_deferral_d1_to_d8_by_number() -> None:
    t = text()
    for n in range(1, 9):
        assert f"D{n}:" in t, n


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
