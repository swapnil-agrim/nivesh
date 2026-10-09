"""Cost meter and monthly budget gate (ST-13.4).

Rule (stated once): at >=80% of the cap `deep` is downgraded to `quick` unless --force; at >=100%
`deep` is refused even with --force (force never overrides the cap). brief/quick always run.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from nivesh_core.config import Price
from nivesh_core.timeutil import to_iso


def run_cost_inr(
    model: str | None,
    in_tok: int,
    out_tok: int,
    prices: dict[str, Price],
    usd_inr: float,
    sdk_cost_usd: float | None,
) -> tuple[float, str]:
    """(INR cost, source): price table first, else the SDK's own USD figure, else 0."""
    if model in prices:
        p = prices[model]
        usd = (in_tok * p.input_usd_per_mtok + out_tok * p.output_usd_per_mtok) / 1_000_000
        return usd * usd_inr, "table"
    if sdk_cost_usd:
        return sdk_cost_usd * usd_inr, "sdk"
    return 0.0, "none"


def month_to_date(conn: sqlite3.Connection, now: datetime) -> float:
    now = now.astimezone(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_inr + paid_data_inr), 0) FROM run "
        "WHERE started_at >= ? AND started_at < ?",
        (to_iso(start), to_iso(nxt)),
    ).fetchone()
    return float(row[0])


@dataclass(frozen=True)
class Decision:
    allowed: bool
    tier: str
    warn: bool
    message: str


def gate(spent: float, cap: float | None, tier: str, force: bool) -> Decision:
    if not cap:
        return Decision(True, tier, False, "")
    pct = spent / cap * 100
    if pct < 80:
        return Decision(True, tier, False, "")
    note = f"monthly cost {spent:.0f}/{cap:.0f} INR ({pct:.0f}% of cap)"
    if pct >= 100:
        if tier == "deep":
            return Decision(
                False, tier, True, f"{note}: deep runs refused (--force cannot override)"
            )
        return Decision(True, tier, True, f"{note}: cap reached")
    if tier == "deep" and not force:
        return Decision(True, "quick", True, f"{note}: deep downgraded to quick (use --force)")
    return Decision(True, tier, True, note)
