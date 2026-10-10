from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nivesh_core.thesis import KillCriterion, Thesis, default_review_date

D = Decimal
DAY = date(2026, 10, 1)


def crit(i: int, machine: bool = True, **kw: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"criterion_id": i, "text": f"criterion {i} breaks"}
    if machine:
        c.update(metric="roce_pct", comparator="lt", threshold=D("12.5"), unit="%")
    c.update(kw)
    return c


def thesis(**kw: Any) -> Thesis:
    base: dict[str, Any] = {
        "security_id": 1,
        "created_at": DAY,
        "horizon": "long_term_1y_plus",
        "why": "Durable franchise with steady returns on capital.",
        "kill_criteria": [crit(1), crit(2, machine=False)],
        "review_date": date(2026, 12, 30),
    }
    base.update(kw)
    return Thesis.model_validate(base)


def test_valid_thesis_with_two_and_with_four_criteria() -> None:
    t = thesis()
    assert t.status == "active" and t.source == "onboarding" and t.thesis_id is None
    assert len(thesis(kill_criteria=[crit(i) for i in range(1, 5)]).kill_criteria) == 4
    assert t.kill_criteria[0].threshold == D("12.5")


@pytest.mark.parametrize("n", [0, 1, 5])
def test_zero_one_and_five_criteria_rejected(n: int) -> None:
    with pytest.raises(ValidationError):
        thesis(kill_criteria=[crit(i) for i in range(1, n + 1)])


def test_why_over_60_words_rejected_and_exactly_60_accepted() -> None:
    assert len(thesis(why=" ".join(["w"] * 60)).why.split()) == 60
    with pytest.raises(ValidationError, match="60 words"):
        thesis(why=" ".join(["w"] * 61))
    with pytest.raises(ValidationError):
        thesis(why="  ")


def test_criterion_metric_triple_is_all_or_none() -> None:
    assert KillCriterion(criterion_id=1, text="t").metric is None
    with pytest.raises(ValidationError, match="all or none"):
        KillCriterion(criterion_id=1, text="t", metric="roce_pct")
    with pytest.raises(ValidationError, match="all or none"):
        KillCriterion(criterion_id=1, text="t", metric="roce_pct", comparator="lt")
    with pytest.raises(ValidationError):
        KillCriterion(criterion_id=1, text="t", metric="m", comparator="eq", threshold=D(1))


def test_at_least_one_machine_checkable_criterion_required() -> None:
    with pytest.raises(ValidationError, match="machine"):
        thesis(kill_criteria=[crit(1, machine=False), crit(2, machine=False)])


def test_duplicate_criterion_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        thesis(kill_criteria=[crit(1), crit(1)])


def test_empty_criterion_text_rejected() -> None:
    with pytest.raises(ValidationError):
        thesis(kill_criteria=[crit(1), crit(2, text=" ")])
    with pytest.raises(ValidationError, match="40 words"):
        thesis(kill_criteria=[crit(1), crit(2, text=" ".join(["w"] * 41))])
    with pytest.raises(ValidationError):
        thesis(kill_criteria=[crit(1), crit(0)])


def test_horizon_limited_to_the_two_verdict_labels() -> None:
    assert thesis(horizon="positional_1_6m").horizon == "positional_1_6m"
    with pytest.raises(ValidationError):
        thesis(horizon="forever")


def test_default_review_date_is_created_plus_configured_days() -> None:
    assert default_review_date(DAY, 90) == date(2026, 12, 30)
    assert default_review_date(DAY, 30) == date(2026, 10, 31)
