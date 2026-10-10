from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0010-portfolio-review.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "thesis", "0006", "forward-only", "restore", "review_rules", "not_evaluable",
        "action_floor", "override_reason", "HOLD", "`schema_version` is `2`", "ThesisDraft",
        "tax.india", "not tax advice", "Profile.tax_rates", "no dated lots", "FIFO", "band edge",
        "turnover", "per proposal", "no new MCP server", "quick shows a warning",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_owner_decision_od1_to_od8_and_each_deferral_x1_to_x9() -> None:
    t = text()
    for n in range(1, 9):
        assert f"OD-{n} " in t, n
    for n in range(1, 10):
        assert f"X{n}:" in t, n


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
