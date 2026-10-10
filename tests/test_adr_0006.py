from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0006-us-holdings.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "CSV first",
        "`lot` table",
        "nullable `value_inr`",
        "never zero, never one",
        "allow-list",
        "Decimal bisection",
        "no tax advice",
    ):
        assert needle in t, needle


def test_adr_states_fx_source_honesty_and_fred_leg_only() -> None:
    t = text()
    assert "RBI reference rate not wired" in t
    assert "AC3 (ST-3.3) is met on the FRED leg only" in t


def test_adr_lists_each_deferral_d1_to_d6_by_number() -> None:
    t = text()
    for n in range(1, 8):
        assert f"D{n}:" in t, n


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
