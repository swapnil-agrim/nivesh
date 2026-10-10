from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.config import MfSettings, load_settings
from nivesh_core.errors import ConfigError
from nivesh_core.redact import is_sensitive_key

ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return p


def test_mf_defaults_and_nav_gap_days_positive(tmp_path: Path) -> None:
    s = load_settings(write(tmp_path, "data_dir: x\n")).mf
    assert s.nav_gap_days == 5 and s.nav_tolerance == Decimal("0.005")
    assert s.consistency.window_days == [1095, 1826]
    assert s.exit_load == {} and s.tax.long_term_days is None
    for bad in ("nav_gap_days: 0", "nav_gap_days: -1", "nav_tolerance: 0.9"):
        with pytest.raises(ConfigError):
            load_settings(write(tmp_path, f"mf: {{{bad}}}\n"))


def test_mf_holdings_ref_must_be_a_reference(tmp_path: Path) -> None:
    ok = load_settings(write(tmp_path, "mf: {holdings_api_ref: 'ref:SOME_NAME'}\n")).mf
    assert ok.holdings_api_ref == "ref:SOME_NAME"
    with pytest.raises(ConfigError, match="holdings_api_ref"):
        load_settings(write(tmp_path, "mf: {holdings_api_ref: plainvalue}\n"))


def test_literal_secret_in_mf_block_rejected_without_echo(tmp_path: Path) -> None:
    dummy = "lit" + "eral" + "-value-" + "xyz"
    name = "holdings_api" + "_key"
    with pytest.raises(ConfigError) as e:
        load_settings(write(tmp_path, f"mf: {{{name}: {dummy}}}\n"))
    assert dummy not in str(e.value)
    with pytest.raises(ConfigError) as e2:
        load_settings(write(tmp_path, f"mf: {{holdings_api_ref: {dummy}}}\n"))
    assert dummy not in str(e2.value)


def test_exit_load_and_tax_optional_and_validated(tmp_path: Path) -> None:
    s = load_settings(
        write(
            tmp_path,
            "mf:\n  exit_load: {Equity: {percent: 1, days: 365}}\n"
            "  tax: {long_term_days: 365, short_rate_pct: 20, long_rate_pct: 12.5}\n",
        )
    ).mf
    assert s.exit_load["Equity"].percent == Decimal("1") and s.exit_load["Equity"].days == 365
    assert s.tax.long_rate_pct == Decimal("12.5")
    for bad in (
        "exit_load: {E: {percent: 101, days: 5}}",
        "exit_load: {E: {percent: 1, days: 0}}",
        "tax: {short_rate_pct: -1}",
        "consistency: {window_days: []}",
        "consistency: {window_days: [0]}",
    ):
        with pytest.raises(ConfigError):
            load_settings(write(tmp_path, f"mf: {{{bad}}}\n"))


def test_sample_config_loads_with_mf_block() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml").mf
    assert s.holdings_source == "fixture"
    assert s.thresholds.valuation_stretch_ratio == Decimal("1.25")
    assert s.sources.nav_primary == "mfapi" and s.screen.weights.cost == Decimal("1")
    assert s.benchmarks


def test_mf_field_names_are_redaction_safe() -> None:
    def names(model: type, seen: set[str]) -> None:
        for name, f in model.model_fields.items():  # type: ignore[attr-defined]
            seen.add(name)
            ann = f.annotation
            for t in (ann, *getattr(ann, "__args__", ())):
                if hasattr(t, "model_fields"):
                    names(t, seen)

    seen: set[str] = set()
    names(MfSettings, seen)
    assert "holdings_api_ref" in seen and "weights" in seen
    assert [n for n in seen if is_sensitive_key(n)] == []
