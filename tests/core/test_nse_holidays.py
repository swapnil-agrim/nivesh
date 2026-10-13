from datetime import date
from pathlib import Path

import yaml

from nivesh_core.config import MarketSettings
from nivesh_core.nse_holidays import BUILTIN_YEARS, DEFAULT_NSE_HOLIDAYS, merged_nse_holidays

ROOT = Path(__file__).resolve().parents[2]


def test_builtin_lists_shape() -> None:
    assert BUILTIN_YEARS == (2024, 2025, 2026)
    assert {y: len(v) for y, v in DEFAULT_NSE_HOLIDAYS.items()} == {2024: 16, 2025: 13, 2026: 16}
    for year, days in DEFAULT_NSE_HOLIDAYS.items():
        assert all(d.year == year and d.weekday() < 5 for d in days)


def test_merge_is_union_per_year_and_config_only_years_are_added() -> None:
    extra = date(2026, 7, 1)
    got = merged_nse_holidays({2026: [extra], 2027: [date(2027, 1, 26)]})
    assert got[2026] == DEFAULT_NSE_HOLIDAYS[2026] | {extra}
    assert got[2024] == DEFAULT_NSE_HOLIDAYS[2024] and got[2025] == DEFAULT_NSE_HOLIDAYS[2025]
    assert got[2027] == frozenset({date(2027, 1, 26)})


def test_merge_with_empty_config_is_the_builtin_lists() -> None:
    assert merged_nse_holidays({}) == DEFAULT_NSE_HOLIDAYS


def test_merge_does_not_mutate_the_builtin_data() -> None:
    merged_nse_holidays({2026: [date(2026, 7, 1)]})
    assert date(2026, 7, 1) not in DEFAULT_NSE_HOLIDAYS[2026]


def test_shipped_yaml_matches_builtin_and_has_no_weekend_dates() -> None:
    raw = yaml.safe_load((ROOT / "config" / "nivesh.yaml").read_text())["market"]["nse_holidays"]
    for days in raw.values():
        assert all(d.weekday() < 5 for d in days), "weekend dates are never sessions"
    for year in BUILTIN_YEARS:
        assert frozenset(raw[year]) == DEFAULT_NSE_HOLIDAYS[year]


def test_effective_nse_holidays_on_settings() -> None:
    m = MarketSettings(nse_holidays={2027: [date(2027, 1, 26)]})
    eff = m.effective_nse_holidays()
    assert eff[2027] == frozenset({date(2027, 1, 26)}) and eff[2025] == DEFAULT_NSE_HOLIDAYS[2025]


def test_nse_holidays_field_default_stays_empty() -> None:
    assert MarketSettings().nse_holidays == {}
