"""Macro series maths (ST-4.6). Pure, Decimal only: latest value, last change date, year on year,
and the rates snapshot the agents read. A missing role is reported unavailable, never zero."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

DAILY_STALE_DAYS = 5
MONTHLY_STALE_DAYS = 70
YOY_TOLERANCE_DAYS = 20  # the prior-year point must sit this close to exactly a year back

# snapshot role -> (kind, stale window in days); kind "index" also gets year-on-year
SNAPSHOT_ROLES: dict[str, tuple[str, int]] = {
    "policy_us": ("rate", DAILY_STALE_DAYS),
    "policy_in": ("rate", MONTHLY_STALE_DAYS),
    "y10_us": ("rate", DAILY_STALE_DAYS),
    "y10_in": ("rate", MONTHLY_STALE_DAYS),
    "cpi_us": ("index", MONTHLY_STALE_DAYS),
    "cpi_in": ("index", MONTHLY_STALE_DAYS),
    "usdinr": ("rate", DAILY_STALE_DAYS),
    "vix_us": ("rate", DAILY_STALE_DAYS),
    "crude": ("rate", DAILY_STALE_DAYS),
}


@dataclass(frozen=True)
class Obs:
    date: date
    value: Decimal | None  # None = the source reported a missing value


@dataclass(frozen=True)
class RoleSnap:
    role: str
    available: bool
    value: Decimal | None = None
    as_of: date | None = None
    last_change: date | None = None
    yoy_pct: Decimal | None = None
    stale: bool = False
    reason: str | None = None


def _present(obs: Sequence[Obs]) -> list[Obs]:
    return sorted((o for o in obs if o.value is not None), key=lambda o: o.date)


def latest_value(obs: Sequence[Obs]) -> Obs | None:
    present = _present(obs)
    return present[-1] if present else None


def last_change_date(obs: Sequence[Obs]) -> date | None:
    """Date of the observation where the current value first appeared (None if it never changed)."""
    present = _present(obs)
    for prev, cur in zip(reversed(present[:-1]), reversed(present[1:]), strict=True):
        if prev.value != cur.value:
            return cur.date
    return None


def yoy_pct(obs: Sequence[Obs]) -> Decimal | None:
    """Percent change of the latest value over the one a year earlier (None if absent or zero)."""
    present = _present(obs)
    if not present:
        return None
    last = present[-1]
    try:
        target = last.date.replace(year=last.date.year - 1)
    except ValueError:  # 29 Feb
        target = last.date.replace(year=last.date.year - 1, day=28)
    near = min(present[:-1], key=lambda o: abs(o.date - target), default=None)
    if near is None or abs(near.date - target) > timedelta(days=YOY_TOLERANCE_DAYS):
        return None
    if not near.value or last.value is None:
        return None
    return (last.value / near.value - 1) * 100


def rates_snapshot(series: Mapping[str, Sequence[Obs]], as_of: date) -> dict[str, RoleSnap]:
    out: dict[str, RoleSnap] = {}
    for role, (kind, window) in SNAPSHOT_ROLES.items():
        latest = latest_value(series.get(role, []))
        if latest is None:
            out[role] = RoleSnap(role, False, reason="no data stored; run `nivesh market macro`")
            continue
        obs = series[role]
        out[role] = RoleSnap(
            role,
            True,
            value=latest.value,
            as_of=latest.date,
            last_change=last_change_date(obs) if role.startswith("policy") else None,
            yoy_pct=yoy_pct(obs) if kind == "index" else None,
            stale=(as_of - latest.date).days > window,
        )
    return out
