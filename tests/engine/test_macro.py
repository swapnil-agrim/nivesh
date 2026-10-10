from datetime import date
from decimal import Decimal

from nivesh_engine.macro import (
    Obs,
    last_change_date,
    latest_value,
    rates_snapshot,
    yoy_pct,
)

D = Decimal


def obs(*pairs: tuple[str, str | None]) -> list[Obs]:
    return [Obs(date.fromisoformat(d), None if v is None else D(v)) for d, v in pairs]


def test_last_change_date_finds_most_recent_differing_value() -> None:
    s = obs(("2025-01-01", "6.5"), ("2025-02-01", "6.5"), ("2025-03-01", "6.25"),
            ("2025-04-01", "6.25"), ("2025-05-01", "6.25"))  # fmt: skip
    assert last_change_date(s) == date(2025, 3, 1)


def test_last_change_date_none_when_constant() -> None:
    assert last_change_date(obs(("2025-01-01", "6.5"), ("2025-02-01", "6.5"))) is None
    assert last_change_date([]) is None
    assert last_change_date(obs(("2025-01-01", "6.5"))) is None


def test_yoy_from_index_series_twelve_months_back() -> None:
    s = obs(("2025-01-01", "100"), ("2025-06-01", "103"), ("2026-01-01", "105"))
    assert yoy_pct(s) == D("5")


def test_yoy_missing_prior_year_is_none() -> None:
    assert yoy_pct(obs(("2025-06-01", "103"), ("2026-01-01", "105"))) is None
    assert yoy_pct([]) is None
    assert yoy_pct(obs(("2025-01-01", "0"), ("2026-01-01", "5"))) is None


def test_latest_value_skips_missing_dots() -> None:
    s = obs(("2026-01-02", "4.5"), ("2026-01-05", None))
    assert latest_value(s) == Obs(date(2026, 1, 2), D("4.5"))
    assert latest_value(obs(("2026-01-05", None))) is None


def test_rates_snapshot_assembles_roles_and_marks_missing_roles_unavailable() -> None:
    series = {
        "policy_us": obs(("2026-01-01", "4.5"), ("2026-01-05", "4.25")),
        "y10_us": obs(("2026-01-05", "4.1")),
        "cpi_us": obs(("2025-01-01", "100"), ("2026-01-01", "103")),
    }
    snap = rates_snapshot(series, as_of=date(2026, 1, 6))
    assert snap["policy_us"].value == D("4.25") and snap["policy_us"].last_change == date(
        2026, 1, 5
    )
    assert snap["y10_us"].value == D("4.1") and snap["y10_us"].last_change is None
    assert snap["cpi_us"].yoy_pct == D("3") and snap["cpi_us"].value == D("103")
    missing = snap["policy_in"]
    assert missing.available is False and missing.value is None and missing.reason
    assert set(snap) == {"policy_us", "policy_in", "y10_us", "y10_in", "cpi_us", "cpi_in",
                         "usdinr", "vix_us", "crude"}  # fmt: skip


def test_stale_observation_older_than_5_days_marks_series_stale_in_snapshot() -> None:
    series = {"y10_us": obs(("2026-01-02", "4.1")), "usdinr": obs(("2026-01-05", "90.1"))}
    snap = rates_snapshot(series, as_of=date(2026, 1, 8))
    assert snap["y10_us"].stale is True and snap["usdinr"].stale is False
    monthly = {"cpi_in": obs(("2025-01-01", "100"), ("2025-12-01", "104"))}
    assert rates_snapshot(monthly, as_of=date(2026, 1, 8))["cpi_in"].stale is False  # monthly
