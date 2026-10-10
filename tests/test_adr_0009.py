from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0009-research-committee.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "claude-agent-sdk", "pydantic", "no new dependency", "exactly one repair", "tool_call_id",
        "snapshot", "before the verdict", "Semaphore", "verdict_ceiling", "veto",
        "INSUFFICIENT_DATA", "coverage", "`engine` server", "nivesh-engine", "ten", "read-only",
        "unverified", "resume", "output_format", "weights_version", "GUARD", "key_points",
        "redact_json",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_deferral_by_number() -> None:
    t = text()
    for n in range(1, 15):
        assert f"D{n}:" in t, n


def test_adr_records_the_semantics_the_implementation_chose() -> None:
    t = text()
    for needle in (
        "min(", "strictly", "filing_section_cap", "risk_unavailable", "pm_unavailable",
        "overrides", "fund path",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_supersedes_adr_0008_d1_and_records_the_nivesh_mf_deviation() -> None:
    t = text()
    assert "supersedes ADR-0008 decision 2 (D1)" in t and "ADR-0008 is not edited" in t
    assert "no separate `nivesh-mf` server" in t and "PID 14.4" in t


def test_adr_has_no_credential_or_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
