"""Owner-set schedule of the unattended jobs (ST-11.1), read from `config/schedule.yaml`.

Every number the scheduler and the alert rules use is a parameter here with a documented
default. Times are IST wall-clock text ("HH:MM"); the stored instants are UTC. Unknown keys are
rejected and no field name looks sensitive. A missing file means the built-in defaults.
"""

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nivesh_core.yamlio import read_mapping, validate

SCHEDULE_FILE = "schedule.yaml"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class JobSpec(BaseModel):
    """One job. `name` is the registry key. `at` is the IST time on the job's day; for a US job
    that runs before the open the computed instant wins and `at` is informational."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    market: Literal["IN", "US", "none"] = "none"
    at: str
    weekday: int | None = Field(default=None, ge=0, le=6)  # Monday is 0
    nth_weekday: Literal[1] | None = None  # 1: only the first such weekday of the month
    trading_days_only: bool = True
    day_offset: Literal[0, 1] = 0  # run this many days after the session date it covers
    grace_minutes: int = Field(default=30, ge=0)  # later than this is stored as late
    catchup_hours: int = Field(default=12, ge=1)  # later than this is skipped as a missed window
    stale_minutes: int = Field(default=60, ge=1)  # a running row older than this is reclaimed
    enabled: bool = True

    @field_validator("at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("must be HH:MM, 24-hour")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "JobSpec":
        if self.nth_weekday is not None and self.weekday is None:
            raise ValueError("nth_weekday needs weekday")
        if self.trading_days_only and self.market == "none":
            raise ValueError("trading_days_only needs a market (IN or US)")
        return self


class AlertSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_cap: int = Field(default=8, ge=1)  # alerts delivered per IST day
    dedupe_sessions: int = Field(default=5, ge=1)  # same rule and security at most once per this
    digest_threshold: int = Field(default=3, ge=1)  # more than this inside the window: one digest
    digest_window_min: int = Field(default=60, ge=1)
    vol_mult: Decimal = Field(default=Decimal("2.5"), gt=0)  # daily move vs 20-day daily stdev
    ter_pp: Decimal = Field(default=Decimal("0.2"), ge=0)  # expense ratio change, percent points


def _job(name: str, at: str, **kw: object) -> JobSpec:
    return JobSpec.model_validate({"name": name, "at": at, **kw})


def default_jobs() -> list[JobSpec]:
    return [
        _job("brief_in", "08:00", market="IN", catchup_hours=3),
        _job("login_nudge", "08:45", market="IN", catchup_hours=2),
        _job("brief_us", "18:30", market="US", catchup_hours=3, enabled=False),
        _job("monitors", "17:30", market="IN"),
        _job("monitors_us", "05:30", market="US", day_offset=1, enabled=False),
        _job("kill_criteria", "22:00", market="IN"),
        _job("rescore", "08:30", weekday=5, trading_days_only=False, catchup_hours=15),
        _job(
            "weekly_review",
            "09:00",
            weekday=5,
            trading_days_only=False,
            catchup_hours=15,
            stale_minutes=240,
        ),  # fmt: skip
        _job(
            "monthly_fund_review",
            "09:00",
            weekday=5,
            nth_weekday=1,
            trading_days_only=False,
            catchup_hours=15,
            stale_minutes=240,
        ),  # fmt: skip
        _job("backup", "07:00", weekday=5, trading_days_only=False, catchup_hours=15),
    ]


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobs: list[JobSpec] = Field(default_factory=default_jobs)
    retries: int = Field(default=2, ge=0)  # attempts after the first
    backoff_s: tuple[int, ...] = (30, 120)  # wait before each retry
    us_lead_min: int = Field(default=30, ge=0)  # US pre-open brief: minutes before the open
    deliver_holdings: bool = False  # send the review summaries; default: saved, not delivered
    min_holidays_warn: int = Field(default=8, ge=0)  # warn when the NSE list is shorter
    mf_stale_days: int = Field(default=7, ge=1)
    alerts: AlertSettings = Field(default_factory=AlertSettings)

    @model_validator(mode="after")
    def _consistent(self) -> "ScheduleSettings":
        if len(self.backoff_s) != self.retries:
            raise ValueError("backoff_s needs one wait per retry")
        if any(w < 0 for w in self.backoff_s):
            raise ValueError("backoff_s must not be negative")
        names = [j.name for j in self.jobs]
        if len(set(names)) != len(names):
            raise ValueError("job names must be unique")
        return self


def load_schedule(config_dir: Path) -> ScheduleSettings:
    """`<config_dir>/schedule.yaml`; the built-in defaults when the file is absent."""
    path = config_dir / SCHEDULE_FILE
    if not path.is_file():
        return ScheduleSettings()
    return validate(ScheduleSettings, read_mapping(path), path)
