import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.config import Settings, load_settings
from nivesh_core.errors import ConfigError
from nivesh_core.redact import is_sensitive_key
from nivesh_core.scoring_config import EvaluationSettings, ScoringSettings

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text("data_dir: x\n" + text)
    return load_settings(p)


def test_defaults() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml").scoring
    assert s == ScoringSettings()
    assert s.horizons_weeks == (1, 4, 13, 26, 52) and s.cost_pct == {
        "IN": D("0.25"),
        "US": D("0.10"),
    }
    assert (s.stale_days, s.min_bucket, s.calibration_min) == (7, 20, 40)
    assert (s.resamples, s.resample_seed, s.primary_horizon_weeks) == (1000, 20260101, 13)
    assert s.benchmarks == {}
    assert load_settings(ROOT / "config" / "nivesh.yaml").evaluation == EvaluationSettings()
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    assert re.search(r"^scoring:", text, re.MULTILINE) and "price index, not TRI" in text


@pytest.mark.parametrize(
    "block",
    [
        "scoring: {surprise: 1}",
        "scoring: {cost_pct: {IN: -1}}",
        "scoring: {cost_pct: {UK: 1}}",
        "scoring: {resample_seed: 0}",
        "scoring: {primary_horizon_weeks: 8}",
        "scoring: {horizons_weeks: [2]}",
        "scoring: {horizons_weeks: []}",
        "scoring: {horizons_weeks: [4, 4]}",
        "scoring: {horizons_weeks: [1, 4], primary_horizon_weeks: 13}",
        "scoring: {min_bucket: 0}",
        "evaluation: {surprise: 1}",
        "evaluation: {model_cutoffs: {m: notadate}}",
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, block: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, block + "\n")


def test_overrides_and_dates_load(tmp_path: Path) -> None:
    s = load(
        tmp_path,
        "scoring: {horizons_weeks: [4, 13], primary_horizon_weeks: 4, benchmarks: {IN: IDX}}\n"
        "evaluation: {model_cutoffs: {model-x: 2025-03-01}}\n",
    )
    assert s.scoring.horizons_weeks == (4, 13) and s.scoring.benchmarks == {"IN": "IDX"}
    assert s.evaluation.model_cutoffs == {"model-x": date(2025, 3, 1)}


def test_no_field_name_trips_the_redaction_helper() -> None:
    names = [*ScoringSettings.model_fields, *EvaluationSettings.model_fields]
    assert not [n for n in names if is_sensitive_key(n)]
