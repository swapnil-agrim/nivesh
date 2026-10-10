"""USDINR conversion for non-INR holdings (ST-3.3). Pure and Decimal-only: no I/O, no clock.

The rate comes only from observations passed in (the stored `usdinr` macro series). A missing rate
is `unavailable`: never zero, never one, never a config value. The cost-meter's exchange rate in
settings is unrelated and is not read here.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from nivesh_core.holdings import Holding

# FRED DEXINUS follows the weekly H.10 cadence, so a gap of several days is normal.
STALE_DAYS = 10
# Valuation refuses a rate older than this: better "unavailable" than a rate from last quarter.
VALUATION_MAX_AGE_DAYS = 30


class FxResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rate: Decimal | None
    rate_date: date | None
    source: str
    stale: bool
    reason: str | None

    @property
    def available(self) -> bool:
        return self.rate is not None


def rate_on_or_before(
    obs: Sequence[tuple[date, Decimal]],
    on: date,
    source: str,
    *,
    stale_days: int = STALE_DAYS,
    max_age_days: int | None = None,
) -> FxResult:
    """The latest positive observation dated on or before `on`.

    `source` is the series label (e.g. `fred:DEXINUS`); the result states honestly that the RBI
    reference rate is not wired."""
    label = f"{source} (RBI reference rate not wired)"
    usable = [(d, v) for d, v in obs if d <= on and v > 0]
    if not usable:
        return FxResult(
            rate=None, rate_date=None, source=label, stale=False,
            reason=f"no USDINR observation on or before {on.isoformat()}",
        )  # fmt: skip
    when, rate = max(usable, key=lambda t: t[0])
    age = (on - when).days
    if max_age_days is not None and age > max_age_days:
        return FxResult(
            rate=None, rate_date=None, source=label, stale=False,
            reason=(
                f"latest USDINR observation {when.isoformat()} is more than {max_age_days} "
                f"days before {on.isoformat()}"
            ),
        )  # fmt: skip
    return FxResult(rate=rate, rate_date=when, source=label, stale=age > stale_days, reason=None)


def convert_holdings(holdings: Sequence[Holding], fx: FxResult) -> list[Holding]:
    """Fill `value_inr` = quantity x price x rate for USD rows; leave it None without a rate."""
    out: list[Holding] = []
    for h in holdings:
        if h.currency == "USD" and fx.rate is not None:
            out.append(h.model_copy(update={"value_inr": h.value_native * fx.rate}))
        else:
            out.append(h)
    return out


def unavailable(reason: str, source: str = "unavailable") -> FxResult:
    """A rate that could not be obtained (series not configured, store busy): never a number."""
    return FxResult(rate=None, rate_date=None, source=source, stale=False, reason=reason)
