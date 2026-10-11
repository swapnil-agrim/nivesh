from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0014-scheduling-and-alerts.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "idempotent", "job_run", "trading date", "catchup_hours", "missed window",
        "CalendarUnknown", "second Sunday", "bars stale", "fund meta stale", "BudgetRefused",
        "no ingest job", "does nothing until", "holiday list", "invalidation_level",
        "schedule.yaml", "deliver_holdings", "5 sessions", "digest", "daily cap",
        "saved, not delivered", "fund_meta_pair", "best effort", "nothing is installed",
        "UTC", "IST", "Accepted gap", "BEGIN IMMEDIATE",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_decision_d1_to_d19_and_each_deferral_x1_to_x9() -> None:
    t = text()
    for n in range(1, 20):
        assert f"D{n} " in t, n
    for n in range(1, 10):
        assert f"X{n}:" in t, n


def test_adr_states_the_send_before_insert_gap_and_the_strict_cap() -> None:
    t = text()
    assert "sent but not recorded" in t and "strict" in t and "next run may send it again" in t


def test_adr_has_no_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
