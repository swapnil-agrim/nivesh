import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nivesh_core.config import Price
from nivesh_core.cost import gate, month_to_date, run_cost_inr
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.timeutil import to_iso

PRICES = {"m": Price(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)}
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def test_cost_table_hit() -> None:
    inr, src = run_cost_inr("m", 1_000_000, 200_000, PRICES, 90.0, 99.0)
    assert src == "table" and inr == pytest.approx((3.0 + 0.2 * 15.0) * 90.0)


def test_cost_falls_back_to_sdk_then_none() -> None:
    assert run_cost_inr("other", 10, 10, PRICES, 90.0, 0.5) == (pytest.approx(45.0), "sdk")
    assert run_cost_inr("other", 10, 10, PRICES, 90.0, None) == (0.0, "none")
    assert run_cost_inr(None, 10, 10, PRICES, 90.0, None) == (0.0, "none")


def seed(conn: sqlite3.Connection, when: datetime, cost: float, paid: float = 0.0) -> None:
    conn.execute(
        "insert into run (command, started_at, status, cost_inr, paid_data_inr) "
        "values ('x', ?, 'ok', ?, ?)",
        (to_iso(when), cost, paid),
    )


def test_month_to_date_current_utc_month_only(tmp_path: Path) -> None:
    init_stores(tmp_path)
    c = open_sqlite(tmp_path / "nivesh.sqlite")
    seed(c, datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC), 1000)  # last month
    seed(c, datetime(2026, 10, 1, tzinfo=UTC), 10, 1)  # boundary, included
    seed(c, datetime(2026, 10, 20, tzinfo=UTC), 5)
    seed(c, datetime(2026, 11, 1, tzinfo=UTC), 1000)  # future month
    assert month_to_date(c, NOW) == pytest.approx(16)
    c.close()


@pytest.mark.parametrize(
    "spent, tier, force, allowed, out_tier, warn",
    [
        (100, "deep", False, True, "deep", False),  # <80%
        (800, "deep", False, True, "quick", True),  # 80%: downgraded
        (800, "deep", True, True, "deep", True),  # --force bypasses the 80% downgrade
        (900, "brief", False, True, "brief", True),
        (1000, "deep", False, False, "deep", True),  # 100%: deep refused
        (1000, "deep", True, False, "deep", True),  # ... even with --force
        (1000, "quick", False, True, "quick", True),
        (1000, "brief", True, True, "brief", True),
    ],
)
def test_gate_rule(
    spent: float, tier: str, force: bool, allowed: bool, out_tier: str, warn: bool
) -> None:
    d = gate(spent, 1000, tier, force)
    assert (d.allowed, d.tier, d.warn) == (allowed, out_tier, warn)
    assert bool(d.message) == warn or not d.allowed


@pytest.mark.parametrize("cap", [0, None])
def test_gate_disabled_without_cap(cap: float | None) -> None:
    d = gate(10**9, cap, "deep", False)
    assert d.allowed and d.tier == "deep" and not d.warn


def test_gate_does_not_leak_into_other_tiers_when_force_and_under_cap() -> None:
    assert gate(0, 1000, "deep", True).tier == "deep"
