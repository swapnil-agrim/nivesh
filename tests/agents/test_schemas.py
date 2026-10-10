import json
from typing import Any

import pytest
from pydantic import ValidationError

from nivesh_agents import schemas
from nivesh_agents.schemas import MODELS, SCHEMA_DIR, CommitteeVerdict, schema_text
from nivesh_core.redact import is_sensitive_key
from tests.agents import committee_fx as fx

NAMES = [
    "AnalystView", "MacroView", "FundView", "DebateTurn", "LensView", "RiskAssessment",
    "HoldingReview", "Verdict",
]  # fmt: skip


def check(name: str, payload: dict[str, Any]) -> Any:
    return MODELS[name].model_validate_json(json.dumps(payload))


def test_generated_schema_files_equal_the_committed_files() -> None:
    assert sorted(MODELS) == sorted(NAMES)
    for n in NAMES:
        assert (SCHEMA_DIR / f"{n}.json").read_text() == schema_text(n), n
    assert sorted(p.name for p in SCHEMA_DIR.glob("*.json")) == sorted(f"{n}.json" for n in NAMES)


def test_each_schema_file_is_valid_json_with_title_and_additional_properties_false() -> None:
    for n in NAMES:
        d = json.loads((SCHEMA_DIR / f"{n}.json").read_text())
        assert d["title"] and d["additionalProperties"] is False and d["type"] == "object"


def test_verdict_schema_title_is_verdict_and_class_is_committee_verdict() -> None:
    assert json.loads((SCHEMA_DIR / "Verdict.json").read_text())["title"] == "Verdict"
    assert MODELS["Verdict"] is CommitteeVerdict and CommitteeVerdict.__name__ == "CommitteeVerdict"


def test_analyst_view_known_valid_payload_validates() -> None:
    v = check("AnalystView", fx.analyst())
    assert v.stance == "bullish" and len(v.key_points) == 3


def test_analyst_view_rejects_unknown_field_bad_enum_score_out_of_range_and_long_thesis() -> None:
    for bad in (
        {"nope": 1}, {"stance": "great"}, {"score": 101}, {"score": -1}, {"confidence": "huge"},
        {"thesis": " ".join(["w"] * 121)}, {"agent": "macro"},
    ):  # fmt: skip
        with pytest.raises(ValidationError):
            check("AnalystView", fx.analyst(**bad))
    check("AnalystView", fx.analyst(thesis=" ".join(["w"] * 120)))


def test_fundamental_view_needs_at_least_three_key_points() -> None:
    with pytest.raises(ValidationError, match="3 key_points"):
        check("AnalystView", fx.analyst(key_points=[fx.point(1), fx.point(2)]))
    check("AnalystView", fx.analyst("news", key_points=[fx.point(1)]))
    check("AnalystView", fx.analyst(stance="insufficient_data", key_points=[], data_gaps=["x"]))


def test_every_key_point_needs_evidence() -> None:
    pts = [fx.point(1), fx.point(2), {"claim": "no proof", "evidence": []}]
    with pytest.raises(ValidationError):
        check("AnalystView", fx.analyst(key_points=pts))


def test_insufficient_data_stance_needs_data_gaps() -> None:
    with pytest.raises(ValidationError, match="data_gaps"):
        check("AnalystView", fx.analyst(stance="insufficient_data", data_gaps=[]))


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("MacroView", fx.macro(), fx.macro(regime="sideways")),
        ("FundView", fx.fund(), fx.fund(action="dance")),
        ("DebateTurn", fx.turn(), fx.turn(side="neutral")),
        ("LensView", fx.lens(), fx.lens(decision="maybe")),
        ("RiskAssessment", fx.risk(), fx.risk(veto="yes")),
        ("HoldingReview", fx.review(), fx.review(thesis_status="great")),
        ("Verdict", fx.verdict(), fx.verdict(verdict="STRONG_BUY")),
    ],
)
def test_macro_fund_debate_lens_risk_review_verdict_each_have_a_valid_and_an_invalid_case(
    name: str, good: dict[str, Any], bad: dict[str, Any]
) -> None:
    check(name, good)
    with pytest.raises(ValidationError):
        check(name, bad)
    with pytest.raises(ValidationError):
        check(name, {**good, "extra_field": 1})


def test_debate_round_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        check("DebateTurn", fx.turn(rnd=0))
    check("DebateTurn", fx.turn(claims=[{"claim": "c", "evidence": [{"tool_call_id": "x"}]}]))


def test_verdict_requires_horizon_review_date_and_vetoed_flag() -> None:
    for drop in ("horizon", "review_date", "vetoed_by_risk", "conviction", "bull_case"):
        p = fx.verdict()
        del p[drop]
        with pytest.raises(ValidationError):
            check("Verdict", p)
    for label in schemas.LADDER:
        check("Verdict", fx.verdict(verdict=label))


def test_entry_zone_ccy_limited_to_inr_or_usd() -> None:
    with pytest.raises(ValidationError):
        check("Verdict", fx.verdict(entry_zone={"low": 1, "high": 2, "ccy": "EUR"}))
    check("Verdict", fx.verdict(entry_zone=None))


def test_strict_json_mode_accepts_iso_dates_and_rejects_numeric_strings_for_numbers() -> None:
    check("AnalystView", fx.analyst(as_of="2026-01-02"))
    for bad in ({"score": "70"}, {"security_id": "1"}, {"as_of": 20260102}):
        with pytest.raises(ValidationError):
            check("AnalystView", fx.analyst(**bad))
    v = check("Verdict", fx.verdict(coverage_pct=87.5))
    assert str(v.coverage_pct) == "87.5"
    # Decimal fields also take an exact digit string (observed behaviour of strict JSON mode)
    assert str(check("Verdict", fx.verdict(coverage_pct="87.5")).coverage_pct) == "87.5"


def test_schema_objects_never_use_a_field_named_with_a_redaction_trigger_except_the_pid_names() -> (
    None
):
    allowed = {"key_points"}
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    seen.update(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for n in NAMES:
        walk(schemas.schema_json(n))
    assert "key_points" in seen
    assert [s for s in seen if is_sensitive_key(s) and s not in allowed] == []
