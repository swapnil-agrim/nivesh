"""Mutual-fund lots, exit-load and gain/days estimates (ST-5.6).

Pure and Decimal-only: no I/O, no clock (`as_of` is a parameter). Lots are built first-in
first-out from the statement transactions. These are informational estimates from owner-set
tables: an unknown exit-load row, an unset tax rate or an unavailable NAV gives "unavailable" with
a reason, never zero. Nothing here tells the owner what to do.

Transaction types follow the statement parser (lowercase): units are added by purchase,
purchase_sip, dividend_reinvest, switch_in, switch_in_merger and gift_in, and removed by
redemption, switch_out, switch_out_merger and gift_out. Cash dividends and tax lines carry no
units and are ignored; segregation, reversal, misc and unknown rows are not modelled and are
reported as warnings because they can change the balance.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from nivesh_core.config import ExitLoad, MfTax
from nivesh_core.holdings import Txn
from nivesh_engine.returns import holding_days, is_long_term

ZERO, HUNDRED, CENT = Decimal(0), Decimal(100), Decimal("0.01")
ADDS = frozenset(
    {"purchase", "purchase_sip", "dividend_reinvest", "switch_in", "switch_in_merger", "gift_in"}
)
REDUCES = frozenset({"redemption", "switch_out", "switch_out_merger", "gift_out"})
NO_UNITS = frozenset({"dividend_payout", "stt_tax", "stamp_duty_tax", "tds_tax"})
NOT_MODELLED = frozenset({"segregation", "reversal", "misc", "unknown"})
UNIT_TOLERANCE = Decimal("0.001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class OpenLot:
    acquired_on: date
    units: Decimal
    cost_per_unit: Decimal | None


@dataclass(frozen=True)
class LotReport:
    lots: list[OpenLot]
    open_units: Decimal
    data_error: str | None = None  # a redemption exceeded the open units: reported, not clamped
    ignored: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _cost_per_unit(t: Txn, units: Decimal) -> Decimal | None:
    if t.amount is not None and t.amount != 0:
        return abs(t.amount) / units
    return t.price if t.price is not None and t.price > 0 else None


def fifo_lots(txns: Sequence[Txn]) -> LotReport:
    """Open lots after replaying the transactions oldest first (adds before reductions on a day)."""
    ordered = sorted(txns, key=lambda t: (t.txn_date, 0 if t.txn_type in ADDS else 1))
    lots: list[OpenLot] = []
    ignored: Counter[str] = Counter()
    warnings: list[str] = []
    error: str | None = None
    for t in ordered:
        kind = t.txn_type
        if kind in NO_UNITS:
            ignored[kind] += 1
            continue
        if kind in NOT_MODELLED or (kind not in ADDS and kind not in REDUCES):
            ignored[kind] += 1
            warnings.append(
                f"{t.txn_date.isoformat()} {kind}: not modelled; the balance may differ"
            )
            continue
        units = abs(t.quantity) if t.quantity else ZERO
        if units == 0:
            warnings.append(f"{t.txn_date.isoformat()} {kind}: no units; row skipped")
            continue
        if kind in ADDS:
            lots.append(OpenLot(t.txn_date, units, _cost_per_unit(t, units)))
            continue
        left = units
        while left > 0 and lots:
            take = min(left, lots[0].units)
            left -= take
            if take == lots[0].units:
                lots.pop(0)
            else:
                first = lots[0]
                lots[0] = OpenLot(first.acquired_on, first.units - take, first.cost_per_unit)
        if left > 0 and error is None:
            error = (
                f"{t.txn_date.isoformat()} {kind} of {units} units exceeds the open units by "
                f"{left}; the history is incomplete"
            )
    return LotReport(
        lots, sum((lot.units for lot in lots), ZERO), error, dict(sorted(ignored.items())), warnings
    )


def reconcile_units(report: LotReport, holding_quantity: Decimal) -> str | None:
    """A note when the lot units differ from the held quantity (the history is then incomplete)."""
    if abs(report.open_units - holding_quantity) > UNIT_TOLERANCE:
        return (
            f"lot units {report.open_units} differ from the held quantity {holding_quantity}; "
            "estimates from lots are unavailable"
        )
    return None


@dataclass(frozen=True)
class ValuedLot:
    acquired_on: date
    units: Decimal
    cost_per_unit: Decimal | None
    holding_days: int
    value: Decimal | None
    cost: Decimal | None
    gain: Decimal | None


@dataclass(frozen=True)
class ValuedLots:
    lots: list[ValuedLot]
    reason: str | None  # why the lots cannot be used (history or NAV problem), else None


def valued_lots(
    report: LotReport,
    nav: Decimal | None,
    nav_date: date | None,
    as_of: date,
    max_nav_age_days: int,
    holding_quantity: Decimal | None = None,
) -> ValuedLots:
    """Lots with holding days, value and gain at the latest NAV; unavailable on stale or no NAV."""
    why: str | None = report.data_error
    if why is None and holding_quantity is not None:
        why = reconcile_units(report, holding_quantity)
    if why is None and (nav is None or nav_date is None):
        why = "no stored NAV"
    elif why is None and nav_date is not None and (as_of - nav_date).days > max_nav_age_days:
        why = f"latest NAV ({nav_date.isoformat()}) is {(as_of - nav_date).days} days old"
    usable = why is None and nav is not None
    out = []
    for lot in report.lots:
        value = _q(lot.units * nav) if usable and nav is not None else None
        cost = _q(lot.units * lot.cost_per_unit) if lot.cost_per_unit is not None else None
        gain = value - cost if value is not None and cost is not None else None
        out.append(
            ValuedLot(
                lot.acquired_on,
                lot.units,
                lot.cost_per_unit,
                holding_days(lot.acquired_on, as_of),
                value,
                cost,
                gain,
            )  # fmt: skip
        )
    return ValuedLots(out, why)


@dataclass(frozen=True)
class ExitLoadResult:
    available: bool
    amount_inr: Decimal | None
    percent: Decimal | None = None
    days: int | None = None
    lots_in_load: int = 0
    reason: str | None = None


def exit_load_estimate(
    lots: ValuedLots, category: str | None, table: Mapping[str, ExitLoad]
) -> ExitLoadResult:
    """Exit load on lots held fewer days than the owner-set schedule for the category."""
    entry = None
    if category:
        entry = next((v for k, v in table.items() if k.casefold() == category.casefold()), None)
    if entry is None:
        return ExitLoadResult(
            False, None, reason="exit load unknown (owner-set table has no entry)"
        )
    if lots.reason is not None or not lots.lots:
        why = lots.reason or "no open lots"
        return ExitLoadResult(False, None, entry.percent, entry.days, reason=why)
    inside = [lot for lot in lots.lots if lot.holding_days < entry.days]
    if any(lot.value is None for lot in inside):  # pragma: no cover - value is set when usable
        return ExitLoadResult(
            False, None, entry.percent, entry.days, reason="lot value unavailable"
        )
    amount = _q(sum((lot.value or ZERO for lot in inside), ZERO) * entry.percent / HUNDRED)
    return ExitLoadResult(True, amount, entry.percent, entry.days, len(inside))


@dataclass(frozen=True)
class LotTax:
    acquired_on: date
    holding_days: int
    gain: Decimal | None
    long_term: bool | None
    tax_inr: Decimal | None
    reason: str | None


@dataclass(frozen=True)
class TaxImpact:
    lots: list[LotTax]
    total_gain: Decimal | None
    total_tax_inr: Decimal | None
    reason: str | None


def tax_impact(lots: ValuedLots, cfg: MfTax) -> TaxImpact:
    """Gain and holding days per lot; a tax amount only where the owner has set the rate."""
    if lots.reason is not None or not lots.lots:
        return TaxImpact([], None, None, lots.reason or "no open lots")
    rows: list[LotTax] = []
    for lot in lots.lots:
        term = is_long_term(lot.holding_days, cfg.long_term_days)
        rate = cfg.long_rate_pct if term else cfg.short_rate_pct
        tax: Decimal | None = None
        if lot.gain is None:
            why: str | None = "gain unavailable (cost unknown)"
        elif term is None:
            why = "long-term threshold not set"
        elif rate is None:
            why = f"{'long' if term else 'short'}-term rate not set"
        else:
            tax, why = _q(max(lot.gain, ZERO) * rate / HUNDRED), None
        rows.append(LotTax(lot.acquired_on, lot.holding_days, lot.gain, term, tax, why))
    gains = [r.gain for r in rows]
    total_gain = None if any(g is None for g in gains) else sum((g or ZERO for g in gains), ZERO)
    taxes = [r.tax_inr for r in rows]
    ok = all(t is not None for t in taxes)
    reason = next((r.reason for r in rows if r.reason), None)
    return TaxImpact(
        rows, total_gain, sum((t or ZERO for t in taxes), ZERO) if ok else None, reason
    )
