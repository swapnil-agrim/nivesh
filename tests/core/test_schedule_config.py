from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from nivesh_core.errors import ConfigError
from nivesh_core.redact import is_sensitive_key
from nivesh_core.schedule_config import (
    AlertSettings,
    JobSpec,
    ScheduleSettings,
    default_jobs,
    load_schedule,
)

ROOT = Path(__file__).resolve().parents[2]


def by_name(s: ScheduleSettings) -> dict[str, JobSpec]:
    return {j.name: j for j in s.jobs}


def test_committed_yaml_equals_defaults() -> None:
    assert load_schedule(ROOT / "config") == ScheduleSettings()


def test_defaults_match_the_spec() -> None:
    s = ScheduleSettings()
    jobs = by_name(s)
    assert (s.retries, s.backoff_s, s.us_lead_min) == (2, (30, 120), 30)
    assert jobs["brief_in"].at == "08:00" and jobs["login_nudge"].at == "08:45"
    assert jobs["brief_in"].grace_minutes == 30 and jobs["brief_in"].market == "IN"
    assert not jobs["brief_us"].enabled and not jobs["monitors_us"].enabled
    assert jobs["monitors_us"].day_offset == 1
    assert jobs["weekly_review"].stale_minutes == jobs["monthly_fund_review"].stale_minutes == 240
    assert jobs["weekly_review"].weekday == 5 and jobs["weekly_review"].at == "09:00"
    assert jobs["monthly_fund_review"].nth_weekday == 1
    assert not jobs["rescore"].trading_days_only and jobs["brief_in"].catchup_hours <= 3
    assert s.alerts == AlertSettings(daily_cap=8, dedupe_sessions=5, digest_threshold=3)
    assert s.alerts.vol_mult == Decimal("2.5") and s.alerts.ter_pp == Decimal("0.2")
    assert not s.deliver_holdings and s.min_holidays_warn == 8 and s.mf_stale_days == 7
    assert [j.name for j in default_jobs()] == [j.name for j in s.jobs]


@pytest.mark.parametrize(
    "data",
    [
        {"surprise": 1},
        {"retries": -1},
        {"retries": 2, "backoff_s": [30]},
        {"retries": 1, "backoff_s": [-5]},
        {"alerts": {"daily_cap": 0}},
        {"alerts": {"surprise": 1}},
        {"jobs": [{"name": "a", "at": "8:00", "market": "IN"}]},
        {"jobs": [{"name": "a", "at": "24:00", "market": "IN"}]},
        {"jobs": [{"name": "A", "at": "08:00", "market": "IN"}]},
        {"jobs": [{"name": "a", "at": "08:00"}]},  # trading days need a market
        {"jobs": [{"name": "a", "at": "08:00", "market": "IN", "nth_weekday": 1}]},
        {"jobs": [{"name": "a", "at": "08:00", "market": "UK"}]},
        {"jobs": [{"name": "a", "at": "08:00", "market": "IN"}] * 2},
    ],
)
def test_bad_values_are_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScheduleSettings.model_validate(data)


def test_no_field_name_looks_sensitive() -> None:
    names = [*ScheduleSettings.model_fields, *JobSpec.model_fields, *AlertSettings.model_fields]
    assert [n for n in names if is_sensitive_key(n)] == []


def test_missing_file_gives_defaults_and_bad_file_is_a_config_error(tmp_path: Path) -> None:
    assert load_schedule(tmp_path) == ScheduleSettings()
    (tmp_path / "schedule.yaml").write_text("retries: [1]\n")
    with pytest.raises(ConfigError):
        load_schedule(tmp_path)
    (tmp_path / "schedule.yaml").write_text("jobs:\n  - {name: x, at: '09:00', market: IN}\n")
    assert [j.name for j in load_schedule(tmp_path).jobs] == ["x"]
