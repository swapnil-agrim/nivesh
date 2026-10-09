from pathlib import Path

import pytest
import yaml

from nivesh_core.errors import ConfigError
from nivesh_core.profile import load_profile

ROOT = Path(__file__).resolve().parents[2]
VALID = {
    "risk_tolerance": "moderate",
    "horizon_split": {"short": 20, "medium": 30, "long": 50},
    "target_allocation": {"equity": 60, "debt": 40},
    "exclusions": ["TOBACCO"],
    "tax_rates": {"stcg": 20, "ltcg": 12.5},
    "monthly_cost_cap": 1000,
}


def write(tmp_path: Path, **over: object) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.safe_dump({**VALID, **over}))
    return p


def test_sample_profile_loads() -> None:
    p = load_profile(ROOT / "config" / "profile.yaml")
    assert p.base_currency == "INR"


def test_defaults(tmp_path: Path) -> None:
    p = load_profile(write(tmp_path))
    assert (p.max_position_pct, p.max_sector_pct, p.base_currency) == (10, 30, "INR")


@pytest.mark.parametrize(
    ("over", "path"),
    [
        ({"max_position_pct": 150}, "max_position_pct"),
        ({"target_allocation": {"equity": 50, "debt": 40}}, "target_allocation"),
        ({"horizon_split": {"short": 10, "long": 10}}, "horizon_split"),
        ({"surprise": 1}, "surprise"),
        ({"base_currency": "rupee"}, "base_currency"),
        ({"risk_tolerance": "yolo"}, "risk_tolerance"),
    ],
)
def test_invalid_names_field_path(tmp_path: Path, over: dict[str, object], path: str) -> None:
    with pytest.raises(ConfigError, match=path):
        load_profile(write(tmp_path, **over))


def test_missing_and_non_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_profile(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_profile(bad)
    bad.write_text("a: [unclosed\n")
    with pytest.raises(ConfigError, match="YAML"):
        load_profile(bad)
