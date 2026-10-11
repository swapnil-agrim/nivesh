"""Engine notes for `/ta` and `/fa`: built from the engine result alone."""

from datetime import UTC, datetime

import pytest

from nivesh_adapters import analysis_service as svc
from nivesh_adapters.report_check import finalize
from nivesh_adapters.research_service import engine_report, note_of
from nivesh_core.errors import NiveshError
from tests.adapters import test_analysis_service as tas
from tests.adapters.test_analysis_service import Stores, args
from tests.cli.test_engine_cli import ASOF

st = tas.st  # the shared seeded stores
RUN_AT = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)


def note(st: Stores, kind: str, query: str):  # type: ignore[no-untyped-def]
    rep, row = engine_report(*args(st), kind, query, ASOF)
    return note_of(rep, row, kind, ASOF, RUN_AT, 5)


def test_ta_note_has_levels_setups_and_data_gaps_from_the_engine_result(st: Stores) -> None:
    report, facts = note(st, "ta", "US1")
    text = "\n".join(b.text for b in report.blocks)
    assert "rsi_14:" in text and "Setup " in text and "Example Tech 1" in report.title
    assert any("rs_benchmark_ratio" in g for g in report.gaps)  # unavailable becomes a gap
    assert report.decision.headers[1] == "Setup" and facts["result"]["setup"]
    res = svc.plain(svc.ta_report(*args(st), "US1", ASOF).result)
    assert res["setup"]["setup"] in text


def test_fa_note_groups_measures_and_lists_unavailable_as_gaps(st: Stores) -> None:
    report, _ = note(st, "fa", "US1")
    heads = [b.heading for b in report.blocks]
    assert heads[:4] == ["Growth", "Profitability", "Balance sheet", "Cash quality"]
    assert "roe_pct:" in next(b.text for b in report.blocks if b.heading == "Profitability")
    assert any("interest_cover" in g for g in report.gaps)


@pytest.mark.parametrize("kind", ["ta", "fa"])
def test_note_is_validated_and_engine_numbers_all_match(st: Stores, kind: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    report, facts = note(st, kind, "US1")
    done = finalize(report, facts, tmp_path)
    assert done.banner == "" and done.unmatched == ()


def test_an_invented_number_in_an_analyst_line_is_flagged(st: Stores, tmp_path) -> None:  # type: ignore[no-untyped-def]
    rep, row = engine_report(*args(st), "ta", "US1", ASOF)
    report, facts = note_of(rep, row, "ta", ASOF, RUN_AT, 5, analyst=("Target 777.77 soon",))
    assert finalize(report, facts, tmp_path).banner


def test_unknown_kind_is_an_error(st: Stores) -> None:
    with pytest.raises(NiveshError, match="no engine note"):
        engine_report(*args(st), "xx", "US1", ASOF)


def test_valuation_lines_split_available_values_from_gaps() -> None:
    from nivesh_adapters.research_service import valuation_lines

    lines, gaps = valuation_lines({
        "multiples": {"multiples": {
            "pe": {"current": {"available": True, "value": "21.456"}},
            "pb": {"current": {"available": False, "reason": "no book value"}},
            "ps": {"current": None},
        }},
        "range": {"scenarios": {
            "base": {"per_share": {"available": True, "value": "150.5"},
                     "growth_pct": "8", "discount_pct": "12"},
            "bear": {"per_share": {"available": False}},
        }},
    })  # fmt: skip
    assert lines == [
        "pe now 21.46",
        "base scenario value per share 150.50 (growth 8%, discount rate 12%)",
    ]
    assert gaps == ["pb: no book value", "ps: unavailable"]
    assert valuation_lines({}) == ([], [])


def test_gather_research_unknown_security_raises_and_missing_data_is_a_gap(st: Stores) -> None:
    from nivesh_adapters.research_service import gather_research

    with pytest.raises(NiveshError, match="not in the security master"):
        gather_research(*args(st), 99999, ASOF)
    rep, row = engine_report(*args(st), "fa", "US1", ASOF)
    facts = gather_research(*args(st), rep.security_id, ASOF)
    assert facts.security.id == rep.security_id
    assert isinstance(facts.gaps, tuple)
