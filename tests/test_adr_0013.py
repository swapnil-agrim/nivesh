from pathlib import Path

from nivesh_core.pii_scan import scan_text

ADR = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0013-ledger-scoring-evaluation.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_core_decisions() -> None:
    t = text()
    for needle in (
        "split-adjusted", "display only", "as_of", "price_date", "append-only",
        "price index, not TRI", "1/4/13/26/52", "not enough data", "non-strict", "rng_state",
        "resample_seed", "model_cutoffs", "5 pp", "baseline", "calendar year", "ledger_decision",
        "ledger_score", "read-only", "SCORE_VERSION", "on_date", "BEGIN IMMEDIATE",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_owner_decision_od1_to_od11_and_each_deferral_x1_to_x6() -> None:
    t = text()
    for n in range(1, 12):
        assert f"OD-{n} " in t, n
    for n in range(1, 7):
        assert f"X{n}:" in t, n


def test_adr_has_no_pii_literal() -> None:
    assert scan_text(ADR.read_text()) == []
