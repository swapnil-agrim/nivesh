from datetime import date

import pytest

from nivesh_core.errors import NiveshError
from nivesh_engine.lookahead import assert_out_of_sample

CUT = {"model-x": date(2025, 3, 1)}


def test_a_start_after_the_cutoff_passes() -> None:
    assert assert_out_of_sample(date(2025, 3, 2), "model-x", CUT) is None


def test_the_cutoff_day_itself_and_earlier_are_refused() -> None:
    for start in (date(2025, 3, 1), date(2024, 1, 1)):
        with pytest.raises(NiveshError, match="in-sample evaluation is refused"):
            assert_out_of_sample(start, "model-x", CUT)


def test_a_cutoff_in_the_future_refuses_a_current_evaluation() -> None:
    with pytest.raises(NiveshError, match="refused"):
        assert_out_of_sample(date(2026, 1, 1), "model-x", {"model-x": date(2030, 1, 1)})


def test_an_unknown_model_is_refused_and_the_message_names_the_setting() -> None:
    with pytest.raises(NiveshError, match="evaluation.model_cutoffs"):
        assert_out_of_sample(date(2030, 1, 1), "model-y", CUT)


def test_an_empty_map_refuses_everything() -> None:
    with pytest.raises(NiveshError, match="no model cutoffs are configured"):
        assert_out_of_sample(date(2030, 1, 1), "model-x", {})
