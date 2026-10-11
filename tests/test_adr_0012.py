from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0012-reports-commands-delivery.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "runs/<date>/<run_id>", "find_run_dir", "legacy", "report_input.json", "report.json",
        "lakh", "crore", "DRAFT", "needs_review", "half unit", "Num", "fails closed", "ref:",
        "MockTransport", "make_client", "no new MCP server", "no new agent", "no new dependency",
        "deterministic brief", "engine-only", "--include-holdings", "Entry-zone numbers",
        "not exempt", "unverified", "kinds and positions only",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_owner_decision_od1_to_od9_and_each_deferral_x1_to_x10() -> None:
    t = text()
    for n in range(1, 10):
        assert f"OD-{n} " in t, n
    for n in range(1, 11):
        assert f"X{n}:" in t, n


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
