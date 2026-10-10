from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0011-idea-generation.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "index_member", "0007", "forward-only", "restore", "owner-supplied", "liquidity", "crore",
        "market_cap", "shares_out", "BR-12", "shortlist_max", "append-only", "corrects_id",
        "reported", "conviction", "no new MCP server", "no new agent", "empty universe",
        "unclassified", "look ahead", "id:<n>", "input hash", "roe", "max_runs",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_owner_decision_od1_to_od12_and_each_deferral_x1_to_x9() -> None:
    t = text()
    for n in range(1, 13):
        assert f"OD-{n} " in t, n
    for n in range(1, 10):
        assert f"X{n}:" in t, n


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
