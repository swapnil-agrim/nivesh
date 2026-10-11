"""The golden harness: loading, per-kind checkers, reasons on failure, the release gate."""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from nivesh_adapters import eval_harness as eh
from nivesh_adapters.eval_harness import (
    Case,
    Outcome,
    Report,
    gate,
    load_baseline,
    run_all,
    run_case,
)
from nivesh_core.errors import NiveshError

D = Decimal
GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden_cases.yaml"


def write(tmp_path: Path, cases: list[dict[str, Any]]) -> Path:
    p = tmp_path / "cases.yaml"
    p.write_text(yaml.safe_dump({"cases": cases}))
    return p


# ---- the shipped set ---------------------------------------------------------------------------
def test_shipped_set_has_at_least_40_unique_cases_with_expectations_covering_every_kind() -> None:
    cases = eh.load_cases(GOLDEN)
    assert len(cases) >= 40 and len({c.id for c in cases}) == len(cases)
    assert all(c.expect for c in cases) and {c.kind for c in cases} == set(eh.CHECKERS)
    assert sum(c.safety for c in cases) >= 10
    assert not any(c.historical for c in cases)  # the guard is exercised by its own kind


def test_shipped_set_passes_with_no_model_and_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def deny(*a: Any, **k: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # an import would raise
    monkeypatch.setattr(socket, "socket", deny)
    report = run_all(eh.load_cases(GOLDEN))
    assert report.failed == () and report.pass_rate_pct == D(100)


# ---- loading -----------------------------------------------------------------------------------
def case(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "c1", "kind": "lookahead",
        "given": {"eval_start": "2025-03-02", "model_id": "m", "cutoffs": {"m": "2025-03-01"}},
        "expect": {"outcome": "allowed"},
    }  # fmt: skip
    base.update(over)
    return base


def test_loader_rejects_bad_files(tmp_path: Path) -> None:
    for bad, msg in (
        ([case(kind="nope")], "unknown kind"),
        ([case(), case()], "duplicate case id"),
        ([case(expect={})], "no expected properties"),
        ([case(expect={"x": 1.5})], "not floats"),
    ):
        with pytest.raises(NiveshError, match=msg):
            eh.load_cases(write(tmp_path, bad))
    (tmp_path / "x.yaml").write_text("- not a mapping\n")
    with pytest.raises(NiveshError, match="top-level"):
        eh.load_cases(tmp_path / "x.yaml")


def test_loader_reads_flags(tmp_path: Path) -> None:
    (c,) = eh.load_cases(write(tmp_path, [case(safety=True, historical=True)]))
    assert (c.safety, c.historical, c.kind) == (True, True, "lookahead")


# ---- a broken expectation fails with a readable reason, for every kind -------------------------
SCORE = {
    "verdict": "BUY", "as_of": "2026-01-05", "price_date": "2026-01-05", "horizon_weeks": 1,
    "cost_pct": "0.25", "today": "2027-01-01",
    "security": [["2026-01-05", "100"], ["2026-01-12", "112"]],
    "benchmark": [["2026-01-05", "200"], ["2026-01-12", "210"]],
}  # fmt: skip
WRONG = {
    "score_call": (SCORE, {"call_excess": "9.9999"}),
    "scorecard_bucket": (
        {"dimension": "verdict", "key": "BUY", "calls": [{"count": 3, "excess": "1"}]},
        {"calls": 4},
    ),
    "calibration_verdict": ({"groups": {"high": [5, 3]}}, {"enough": True}),
    "lookahead": ({"eval_start": "2025-03-01", "model_id": "m", "cutoffs": {"m": "2025-03-01"}},
                  {"outcome": "allowed"}),
    "committee_cap": ({"proposed": "BUY", "veto": True}, {"verdict": "BUY"}),
    "committee_veto": ({"excluded": True}, {"veto": False}),
    "momentum_only": ({"momentum": "99"}, {"top_band": True}),
    "citation_check": ({"text": "Target price is 250.00", "evidence": ["1"]}, {"clean": True}),
    "ledger_append_only": (
        {"table": "ledger_score", "op": "delete",
         "insert": "INSERT INTO ledger_score (entry_id, horizon_weeks, status, basis, scored_at) "
                   "VALUES (1, 4, 'scored', 'b', 't')"},
        {"refused": False},
    ),
    "write_name": ({"name": "place_order"}, {"write": False}),
}  # fmt: skip


@pytest.mark.parametrize("kind", sorted(eh.CHECKERS))
def test_every_kind_has_a_failing_on_purpose_case_with_a_reason(kind: str) -> None:
    given, expect = WRONG[kind]
    out = run_case(Case("x", kind, True, False, given, expect))
    assert out.passed is False and "expected" in out.reason and out.safety is True


def test_a_passing_case_says_ok_and_missing_properties_read_as_none() -> None:
    ok = run_case(Case("x", "write_name", False, False, {"name": "get_quote"}, {"write": False}))
    assert ok == Outcome("x", True, "ok", False)
    gone = run_case(Case("x", "write_name", False, False, {"name": "get_quote"}, {"nope": "a"}))
    assert not gone.passed and "got none" in gone.reason


def test_a_checker_crash_is_a_failed_case_not_a_crash() -> None:
    out = run_case(Case("x", "score_call", False, False, {}, {"status": "scored"}))
    assert not out.passed and out.reason.startswith("checker error: KeyError")


def test_scorecard_bucket_not_found() -> None:
    given = {"dimension": "verdict", "key": "SELL", "calls": []}
    miss = run_case(Case("x", "scorecard_bucket", False, False, given, {"found": False}))
    assert miss.passed


def test_ledger_checker_reports_a_missing_trigger_as_not_refused() -> None:
    # a table without triggers lets the statement through
    out = run_case(Case("x", "ledger_append_only", False, False,
                        {"table": "security", "op": "update",
                         "insert": "INSERT INTO security (id, symbol, exchange, currency) "
                                   "VALUES (1, 'A', 'X', 'INR')"},
                        {"refused": False}))  # fmt: skip
    assert out.passed  # the security table is not append-only, so the update goes through


# ---- the look-ahead path -----------------------------------------------------------------------
HIST = {"eval_start": "2025-03-01", "model_id": "m"}


def hist_case() -> Case:
    return Case("h", "write_name", True, True, {**HIST, "name": "get_quote"}, {"write": False})


def test_a_historical_case_inside_the_cutoff_is_refused_with_the_guard_message() -> None:
    out = run_case(hist_case(), {"m": date(2025, 3, 1)})
    assert not out.passed and "refused by the look-ahead guard" in out.reason
    assert "in-sample evaluation is refused" in out.reason
    assert not run_case(hist_case()).passed  # no cutoffs configured: refused


def test_a_historical_case_after_the_cutoff_runs() -> None:
    assert run_case(hist_case(), {"m": date(2025, 2, 28)}).passed


# ---- the gate ------------------------------------------------------------------------------------
def rep(rate: str, safety_failed: bool = False) -> Report:
    outs = (Outcome("a", not safety_failed, "x", True), Outcome("b", True, "ok", False))
    return Report(D(rate), outs)


def test_gate_drop_edges() -> None:
    assert gate(rep("96"), D(100)).blocked is False  # exactly 4 pp
    assert gate(rep("95"), D(100)).blocked is False  # exactly 5 pp does not exceed it
    assert gate(rep("94.9"), D(100)).blocked is True
    assert gate(rep("100"), D(100)).blocked is False
    assert gate(rep("100"), D(90)).blocked is False  # an improvement
    blocked = gate(rep("94.9"), D(100))
    assert "5.1 pp below the baseline" in blocked.reason


def test_a_failed_safety_case_blocks_even_at_a_perfect_rate() -> None:
    g = gate(rep("100", safety_failed=True), D(100))
    assert g.blocked and "safety case failed: a" in g.reason
    assert gate(rep("100", safety_failed=True), None).blocked  # also with no baseline


def test_no_baseline_is_report_only() -> None:
    g = gate(rep("10"), None)
    assert g.blocked is False and g.reason == "report only: no baseline"


def test_baseline_file(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "missing.json") is None
    (tmp_path / "b.json").write_text('{"pass_rate_pct": "97.5"}')
    assert load_baseline(tmp_path / "b.json") == D("97.5")


def test_pass_rate_of_an_empty_run_and_a_partial_run() -> None:
    assert run_all([]).pass_rate_pct == D(100)
    two = [
        Case("a", "write_name", False, False, {"name": "get_quote"}, {"write": False}),
        Case("b", "write_name", False, False, {"name": "get_quote"}, {"write": True}),
    ]
    r = run_all(two)
    assert r.pass_rate_pct == D("50.00") and [o.id for o in r.failed] == ["b"]
