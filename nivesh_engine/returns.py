"""Holding period and money-weighted returns (XIRR) for dated US lots (ST-3.4).

Pure and Decimal-only: no I/O, no clock. XIRR is annualised on an actual/365 basis and returns
None, never zero and never a raise, whenever it is not defined: fewer than two flows, no sign
change, all flows on one date, or no root inside the bracket. No tax amount or advice is computed;
`is_long_term` only compares days against an owner-set parameter.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from nivesh_core.holdings import Lot
from nivesh_engine.fx import VALUATION_MAX_AGE_DAYS, rate_on_or_before

ZERO = Decimal(0)
YEAR = Decimal(365)
LOW, HIGH = Decimal("-0.999999"), Decimal(100)
TOLERANCE = Decimal("1e-12")
PRECISION = Decimal("1e-10")
MAX_PRICE_AGE_DAYS = 10  # a terminal price older than this is "no recent stored price"
MAX_LOT_RATE_AGE_DAYS = 7  # a USDINR rate further than this from a lot date is not used
NO_PRICE = "no recent stored price"

Flow = tuple[date, Decimal]
Key = tuple[str, str]  # (symbol, exchange)


def _npv(flows: Sequence[Flow], rate: Decimal) -> Decimal:
    t0 = flows[0][0]
    base = 1 + rate
    return sum(
        (cf / base ** (Decimal((d - t0).days) / YEAR) for d, cf in flows),
        ZERO,
    )


def xirr(flows: Sequence[Flow]) -> Decimal | None:
    """Annualised money-weighted return (a fraction, 0.1 = 10 percent) or None."""
    seq = sorted((f for f in flows if f[1] != 0), key=lambda f: f[0])
    if len(seq) < 2 or seq[0][0] == seq[-1][0]:
        return None
    if not (any(c > 0 for _, c in seq) and any(c < 0 for _, c in seq)):
        return None
    lo, hi = LOW, HIGH
    f_lo, f_hi = _npv(seq, lo), _npv(seq, hi)
    if (f_lo > 0) == (f_hi > 0):
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = _npv(seq, mid)
        if f_mid == 0 or (hi - lo) < TOLERANCE:
            lo = hi = mid
            break
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return ((lo + hi) / 2).quantize(PRECISION)


def holding_days(acquired_on: date, valuation: date) -> int:
    return (valuation - acquired_on).days


def is_long_term(days: int, threshold: int | None) -> bool | None:
    """Informational: days held versus an owner-set threshold; None when no threshold is set."""
    return None if threshold is None else days >= threshold


@dataclass(frozen=True)
class LotLine:
    acquired_on: date
    quantity: Decimal
    cost_per_unit: Decimal
    holding_days: int
    long_term: bool | None


@dataclass(frozen=True)
class SecurityReturn:
    symbol: str
    exchange: str
    lots: list[LotLine]
    holding_quantity: Decimal
    lots_cover_quantity: bool
    price: Decimal | None
    price_date: date | None
    xirr_usd: Decimal | None
    reason_usd: str | None
    xirr_inr: Decimal | None
    reason_inr: str | None
    note: str | None


@dataclass(frozen=True)
class LotReport:
    securities: list[SecurityReturn] = field(default_factory=list)
    overall_xirr_usd: Decimal | None = None
    overall_reason_usd: str | None = None
    overall_xirr_inr: Decimal | None = None
    overall_reason_inr: str | None = None


def _usable_price(
    price: tuple[Decimal, date] | None, valuation: date
) -> tuple[Decimal, date] | None:
    if price is None:
        return None
    close, when = price
    if when > valuation or (valuation - when) > timedelta(days=MAX_PRICE_AGE_DAYS) or close <= 0:
        return None
    return close, when


def lot_report(
    lots: Sequence[Lot],
    holding_qty: Mapping[Key, Decimal],
    prices: Mapping[Key, tuple[Decimal, date] | None],
    valuation: date,
    *,
    long_term_days: int | None,
    usdinr: Sequence[Flow] | None = None,
    usdinr_source: str = "usdinr",
) -> LotReport:
    """Per-security and overall XIRR in USD (and INR when a USDINR series is given)."""
    by_key: dict[Key, list[Lot]] = {}
    for lt in sorted(lots, key=lambda x: x.acquired_on):
        by_key.setdefault((lt.symbol, lt.exchange), []).append(lt)
    keys = sorted(set(by_key) | set(holding_qty))
    secs: list[SecurityReturn] = []
    usd_all: list[Flow] = []
    inr_all: list[Flow] = []
    no_price: list[str] = []
    no_inr: list[str] = []
    for key in keys:
        mine = by_key.get(key, [])
        held = holding_qty.get(key, ZERO)
        covered = sum((x.quantity for x in mine), ZERO)
        lines = [
            LotLine(
                x.acquired_on,
                x.quantity,
                x.cost_per_unit,
                (d := holding_days(x.acquired_on, valuation)),
                is_long_term(d, long_term_days),
            )  # fmt: skip
            for x in mine
        ]
        px = _usable_price(prices.get(key), valuation)
        x_usd = x_inr = None
        why_usd = why_inr = None
        if not mine:
            why_usd = why_inr = "no lot dates"
        elif px is None:
            why_usd = why_inr = NO_PRICE
            no_price.append(key[0])
        else:
            usd = [(x.acquired_on, -x.quantity * x.cost_per_unit) for x in mine]
            usd.append((valuation, covered * px[0]))
            x_usd = xirr(usd)
            why_usd = None if x_usd is not None else "XIRR is not defined for these flows"
            usd_all += usd
            x_inr, why_inr, inr_flows = _inr(
                mine, covered * px[0], valuation, usdinr, usdinr_source
            )
            if inr_flows is None:
                no_inr.append(key[0])
            else:
                inr_all += inr_flows
        secs.append(
            SecurityReturn(
                symbol=key[0],
                exchange=key[1],
                lots=lines,
                holding_quantity=held,
                lots_cover_quantity=bool(mine) and covered >= held,
                price=px[0] if px else None,
                price_date=px[1] if px else None,
                xirr_usd=x_usd,
                reason_usd=why_usd,
                xirr_inr=x_inr,
                reason_inr=why_inr,
                note="XIRR covers only dated lots" if mine and covered < held else None,
            )  # fmt: skip
        )
    dated = [s for s in secs if s.lots]
    o_usd, why_o_usd = _overall(usd_all, dated, no_price, "no recent stored price for")
    o_inr, why_o_inr = _overall(
        inr_all, dated, [*no_price, *no_inr], "INR XIRR unavailable, missing price or rate for"
    )
    return LotReport(secs, o_usd, why_o_usd, o_inr, why_o_inr)


def _overall(
    flows: list[Flow], dated: list[SecurityReturn], missing: list[str], prefix: str
) -> tuple[Decimal | None, str | None]:
    if not dated:
        return None, "no lot dates"
    if missing:
        return None, f"{prefix} {', '.join(sorted(set(missing)))}"
    got = xirr(flows)
    return got, None if got is not None else "XIRR is not defined for these flows"


def _inr(
    mine: Sequence[Lot],
    terminal_usd: Decimal,
    valuation: date,
    usdinr: Sequence[Flow] | None,
    source: str,
) -> tuple[Decimal | None, str | None, list[Flow] | None]:
    """(xirr, reason, flows) in INR: each lot cost at the rate of its own date, the terminal value
    at the valuation-date rate. Any missing rate makes INR unavailable, never an assumed one."""
    if usdinr is None:
        return None, "no USDINR series supplied", None
    flows: list[Flow] = []
    for x in mine:
        fx = rate_on_or_before(usdinr, x.acquired_on, source, max_age_days=MAX_LOT_RATE_AGE_DAYS)
        if fx.rate is None:
            why = f"no USDINR rate within {MAX_LOT_RATE_AGE_DAYS} days of {x.acquired_on}"
            return None, why, None
        flows.append((x.acquired_on, -x.quantity * x.cost_per_unit * fx.rate))
    end = rate_on_or_before(usdinr, valuation, source, max_age_days=VALUATION_MAX_AGE_DAYS)
    if end.rate is None:
        return None, end.reason, None
    flows.append((valuation, terminal_usd * end.rate))
    got = xirr(flows)
    return got, None if got is not None else "XIRR is not defined for these flows", flows
