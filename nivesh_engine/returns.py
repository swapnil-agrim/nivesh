"""Holding period and money-weighted returns (XIRR) for dated US lots (ST-3.4).

Pure and Decimal-only: no I/O, no clock. XIRR is annualised on an actual/365 basis and returns
None, never zero and never a raise, whenever it is not defined: fewer than two flows, no sign
change, all flows on one date, or no root below the annual-rate ceiling. `xirr_result` also says
which. The solver is bracketed bisection: the bracket starts at (-0.999999, 100] and its upper end
grows tenfold until the sign changes, up to `MAX_RATE`. A security whose dated lots exceed the
held quantity (coverage "over") gets no XIRR and the overall figure is withheld, naming it. No tax
amount or advice is computed; `is_long_term` only compares days against an owner-set parameter.
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
MAX_RATE = Decimal("1e9")  # annual-rate ceiling for the bracket expansion
TOLERANCE = Decimal("1e-12")
PRECISION = Decimal("1e-10")
MAX_PRICE_AGE_DAYS = 10  # a terminal price older than this is "no recent stored price"
MAX_LOT_RATE_AGE_DAYS = 7  # a USDINR rate further than this from a lot date is not used
NO_PRICE = "no recent stored price"
REASON_FEW_FLOWS = "XIRR needs at least two non-zero flows"
REASON_ONE_DATE = "XIRR is undefined: all flows fall on one date"
REASON_NO_POSITIVE = "XIRR is undefined: no positive flow"
REASON_NO_NEGATIVE = "XIRR is undefined: no negative flow"
REASON_CEILING = f"no XIRR root below the {MAX_RATE:f} annual-rate ceiling"
REASON_OVER = "dated lots exceed the held quantity (over-covered); XIRR withheld"

Flow = tuple[date, Decimal]
Key = tuple[str, str]  # (symbol, exchange)


def _npv(flows: Sequence[Flow], rate: Decimal) -> Decimal:
    t0 = flows[0][0]
    base = 1 + rate
    return sum(
        (cf / base ** (Decimal((d - t0).days) / YEAR) for d, cf in flows),
        ZERO,
    )


@dataclass(frozen=True)
class XirrResult:
    rate: Decimal | None
    reason: str | None  # why the rate is unavailable; None when it is available


def _bisect(seq: Sequence[Flow], lo: Decimal, hi: Decimal, f_lo: Decimal) -> Decimal:
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


def xirr_result(flows: Sequence[Flow]) -> XirrResult:
    """Annualised money-weighted return (a fraction, 0.1 = 10 percent) with the reason when none."""
    seq = sorted((f for f in flows if f[1] != 0), key=lambda f: f[0])
    if len(seq) < 2:
        return XirrResult(None, REASON_FEW_FLOWS)
    if seq[0][0] == seq[-1][0]:
        return XirrResult(None, REASON_ONE_DATE)
    if not any(c > 0 for _, c in seq):
        return XirrResult(None, REASON_NO_POSITIVE)
    if not any(c < 0 for _, c in seq):
        return XirrResult(None, REASON_NO_NEGATIVE)
    try:
        f_lo = _npv(seq, LOW)
        hi = HIGH
        while True:
            f_hi = _npv(seq, hi)
            if f_hi == 0:
                return XirrResult(hi.quantize(PRECISION), None)
            if (f_lo > 0) != (f_hi > 0):
                return XirrResult(_bisect(seq, LOW, hi, f_lo), None)
            if hi >= MAX_RATE:
                return XirrResult(None, REASON_CEILING)
            hi = min(hi * 10, MAX_RATE)
    except ArithmeticError:  # an overflowing power means the root is beyond any usable rate
        return XirrResult(None, REASON_CEILING)


def xirr(flows: Sequence[Flow]) -> Decimal | None:
    """Annualised money-weighted return (a fraction, 0.1 = 10 percent) or None."""
    return xirr_result(flows).rate


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
    coverage: str = "none"  # none | exact | partial | over (dated lots versus held quantity)


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
    over: list[str] = []
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
        coverage = coverage_state(bool(mine), covered, held)
        if not mine:
            why_usd = why_inr = "no lot dates"
        elif coverage == "over":
            why_usd = why_inr = REASON_OVER
            over.append(key[0])
        elif px is None:
            why_usd = why_inr = NO_PRICE
            no_price.append(key[0])
        else:
            usd = [(x.acquired_on, -x.quantity * x.cost_per_unit) for x in mine]
            usd.append((valuation, covered * px[0]))
            res = xirr_result(usd)
            x_usd, why_usd = res.rate, res.reason
            usd_all += usd
            x_inr, why_inr, inr_flows = lot_flows_inr(
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
                lots_cover_quantity=coverage == "exact",
                price=px[0] if px else None,
                price_date=px[1] if px else None,
                xirr_usd=x_usd,
                reason_usd=why_usd,
                xirr_inr=x_inr,
                reason_inr=why_inr,
                note=_note(coverage),
                coverage=coverage,
            )  # fmt: skip
        )
    dated = [s for s in secs if s.lots]
    o_usd, why_o_usd = _overall(usd_all, dated, no_price, "no recent stored price for", over)
    o_inr, why_o_inr = _overall(
        inr_all,
        dated,
        [*no_price, *no_inr],
        "INR XIRR unavailable, missing price or rate for",
        over,
    )
    return LotReport(secs, o_usd, why_o_usd, o_inr, why_o_inr)


def coverage_state(has_lots: bool, covered: Decimal, held: Decimal) -> str:
    """Exact Decimal comparison, no tolerance: none, exact, partial or over."""
    if not has_lots:
        return "none"
    return "exact" if covered == held else "partial" if covered < held else "over"


def _note(coverage: str) -> str | None:
    if coverage == "partial":
        return "XIRR covers only dated lots"
    return REASON_OVER if coverage == "over" else None


def _overall(
    flows: list[Flow],
    dated: list[SecurityReturn],
    missing: list[str],
    prefix: str,
    over: Sequence[str] = (),
) -> tuple[Decimal | None, str | None]:
    if not dated:
        return None, "no lot dates"
    if over:
        return None, f"dated lots exceed the held quantity for {', '.join(sorted(set(over)))}"
    if missing:
        return None, f"{prefix} {', '.join(sorted(set(missing)))}"
    res = xirr_result(flows)
    return res.rate, res.reason


def lot_flows_inr(
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
    res = xirr_result(flows)
    return res.rate, res.reason, flows
