"""Look-ahead guard for model-based historical evaluation (ST-12.7).

A model that was trained on the period it is asked to judge has seen the answer. This guard
refuses such an evaluation: the evaluation must start strictly after the model's training
cutoff, and a model with no recorded cutoff is refused. Pure: dates are parameters.
"""

from collections.abc import Mapping
from datetime import date

from nivesh_core.errors import NiveshError


def assert_out_of_sample(eval_start: date, model_id: str, cutoffs: Mapping[str, date]) -> None:
    """Return quietly when `eval_start` is after the cutoff of `model_id`; raise otherwise."""
    if not cutoffs:
        raise NiveshError(
            "no model cutoffs are configured (evaluation.model_cutoffs): an evaluation that "
            "may overlap the model's training data is refused"
        )
    cutoff = cutoffs.get(model_id)
    if cutoff is None:
        raise NiveshError(
            f"model {model_id!r} has no entry in evaluation.model_cutoffs: its training data "
            "may overlap the evaluation, so it is refused"
        )
    if eval_start <= cutoff:
        raise NiveshError(
            f"evaluation from {eval_start} is not after the training cutoff {cutoff} of "
            f"{model_id!r}: in-sample evaluation is refused"
        )
