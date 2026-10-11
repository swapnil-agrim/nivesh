import re
from pathlib import Path

import pytest

from nivesh_core.config import Settings, load_settings
from nivesh_core.errors import ConfigError
from nivesh_core.redact import is_sensitive_key
from nivesh_core.report_config import ReportSettings

ROOT = Path(__file__).resolve().parents[2]


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return load_settings(p)


def test_defaults_validate_without_report_block(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\n")
    assert s.report == ReportSettings() and s.report.max_tolerance_digits == 6


def test_example_block_equals_defaults() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.report == ReportSettings()
    assert re.search(r"^report:", (ROOT / "config" / "nivesh.yaml").read_text(), re.M)


def test_unknown_key_rejected_and_bounds_enforced(tmp_path: Path) -> None:
    for bad in ("report: {surprise: 1}\n", "report: {max_tolerance_digits: -1}\n",
                "report: {max_tolerance_digits: 11}\n"):  # fmt: skip
        with pytest.raises(ConfigError):
            load(tmp_path, bad)
    assert load(tmp_path, "report: {max_tolerance_digits: 0}\n").report.max_tolerance_digits == 0


def test_no_field_name_looks_sensitive() -> None:
    assert [n for n in ReportSettings.model_fields if is_sensitive_key(n)] == []
