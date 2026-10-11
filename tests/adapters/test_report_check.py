import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from nivesh_adapters.report import BANNER, Block, Num, Report, Table, to_html, to_markdown
from nivesh_adapters.report_check import (
    NEEDS_REVIEW,
    apply_validation,
    evidence_from_run,
    finalize,
)
from nivesh_core.paths import run_dir
from nivesh_core.trace import Tracer
from nivesh_engine.citations import Evidence, Validation, validate
from tests.report_fx import base_report

D = Decimal


@dataclass
class Fake:
    status: str = "ok"


def make_run(tmp_path: Path) -> tuple[Path, Tracer]:
    d = run_dir(tmp_path, 1)
    tr = Tracer(d, 1)
    tr.tool_call("toolu_0001", "mcp__engine__ta_compute", {}, agent="technical", security_id=1)
    out = json.dumps({"rsi": 55.5, "close": "101.25", "as_of": "2026-01-02"})
    tr.tool_result("toolu_0001", out, agent="technical", security_id=1)
    tr.tool_result("toolu_0002", json.dumps({"big": 123456789012, "x": 9.5}), security_id=2)
    snap = {"as_of": "2026-01-02", "securities": [{"security_id": 77}],
            "engine_outputs": {"1": {"score_card": {"composite": "62.5"}}},
            "coverage": {"1": {"inputs_available": 4, "inputs_total": 5}}}  # fmt: skip
    (d / "snapshot.json").write_text(json.dumps(snap))
    return d, tr


def report_with(text: str, **over: object) -> Report:
    return base_report(
        summary=(), decision=Table(("a",), ()), blocks=(Block("b1", "Body", text, True, "1"),),
        nums=(), dates=(), **over,
    )  # fmt: skip


def test_evidence_pool_has_trace_tool_results_snapshot_engine_outputs_and_report_input(
    tmp_path: Path,
) -> None:
    d, _ = make_run(tmp_path)
    ev = evidence_from_run(
        d, report_with("x"), {"close_prev": "100.5", "by_security": {"1": {"p": "7.75"}}}
    )
    for text in ("55.5", "101.25", "62.5", "7.75", "2026-01-02", "4"):
        assert not validate([("t", "1", text)], ev).unmatched, text
    assert validate(
        [("t", "1", "9.5")], ev
    ).unmatched  # security 2's tool result is not security 1's
    assert not validate([("t", "2", "9.5")], ev).unmatched
    assert validate([("t", "all", "77")], ev).unmatched  # ids in the snapshot are not evidence


def test_redacted_trace_text_is_not_counted_as_evidence(tmp_path: Path) -> None:
    d = run_dir(tmp_path, 2)
    tr = Tracer(d, 2)
    tr.tool_result("toolu_0003", "holding [REDACTED]8 and total 5.5", security_id=1)
    ev = evidence_from_run(d, report_with("x"), {})
    v = validate([("b", "1", "total 5.5 and 8")], ev)
    assert [m.figure for m in v.unmatched] == ["8"] and "redacted" in v.unmatched[0].reason
    assert evidence_from_run(run_dir(tmp_path, 3), report_with("x"), {}).redacted is False
    tr.tool_result("toolu_0004", json.dumps({"big": 123456789012}), security_id=1)
    assert validate(
        [("b", "1", "123456789012")], evidence_from_run(d, report_with("x"), {})
    ).unmatched


def test_unmatched_number_stamps_draft_banner_first_in_md_and_html(tmp_path: Path) -> None:
    d, _ = make_run(tmp_path)
    r = finalize(report_with("close 101.25 but target 150.75"), {}, d)
    assert r.banner == BANNER
    md, page = to_markdown(r), to_html(r)
    assert md.startswith(f"> **{BANNER}**") and '<div class="draft"><strong>' + BANNER in page
    assert page.index(BANNER) < page.index("<h1>")


def test_banner_lists_each_unmatched_number_with_context(tmp_path: Path) -> None:
    d, _ = make_run(tmp_path)
    r = finalize(report_with("close 101.25, target 150.75 and stop 90.5"), {}, d)
    assert len(r.unmatched) == 2
    assert "150.75 in b1: close 101.25, target 150.75 and stop 90.5" in r.unmatched[0]
    assert "> - 150.75 in b1" in to_markdown(r) and "90.5 in b1" in to_html(r)


def test_clean_report_has_no_banner(tmp_path: Path) -> None:
    d, _ = make_run(tmp_path)
    r = finalize(report_with("close 101.25 and rsi 55.5 on 2026-01-02"), {}, d)
    assert r.banner == "" and r.unmatched == () and not to_markdown(r).startswith(">")


def test_banner_text_is_exactly_DRAFT_dash_UNVERIFIED_NUMBERS() -> None:
    assert BANNER == "DRAFT - UNVERIFIED NUMBERS"
    stamped = apply_validation(report_with("x"), Validation(1, ()))
    assert stamped.banner == ""


def test_model_number_absent_from_report_input() -> None:
    # evidence facts are code-produced; a verdict-bearing report is never a source
    r = base_report(summary=("Target price 777.7",))
    ev = evidence_from_run(Path("/nonexistent-run-dir"), r, {"close": "101.25"})
    assert validate([("s", "all", "Target price 777.7")], ev).unmatched
    assert not validate([("s", "all", "62.5")], ev).unmatched  # a registered number matches


def test_registered_nums_and_dates_join_the_pool(tmp_path: Path) -> None:
    d = run_dir(tmp_path, 4)
    r = base_report(
        summary=("Net debt ₹1.20 crore on 2026-01-02 and 2026-04-02",), blocks=(),
        decision=Table(("a",), ()), dates=(date(2026, 4, 2),), nums=(Num.inr(D("12000000")),),
    )  # fmt: skip
    assert finalize(r, {}, d).banner == ""


def test_finalize_sets_metered_status_needs_review_when_unmatched(tmp_path: Path) -> None:
    d, _ = make_run(tmp_path)
    m = Fake()
    out = finalize(report_with("invented 888.8"), {}, d, metered=m)
    assert m.status == NEEDS_REVIEW and out.banner


def test_finalize_keeps_ok_when_clean(tmp_path: Path) -> None:
    d, _ = make_run(tmp_path)
    m = Fake()
    finalize(report_with("close 101.25"), {}, d, metered=m)
    assert m.status == "ok"


def test_finalize_never_hides_an_error(tmp_path: Path) -> None:
    d, _ = make_run(tmp_path)
    m = Fake("error")
    finalize(report_with("invented 888.8"), {}, d, metered=m)
    assert m.status == "error"


def test_empty_evidence_object_is_usable() -> None:
    assert Evidence().numbers == {}
