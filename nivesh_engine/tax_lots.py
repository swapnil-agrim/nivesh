"""Per-lot holding period, long-term status and estimated tax from owner-set rates (ST-8.3).

Pure and Decimal-only: no I/O, no clock (`as_of` is a parameter). Rates, the long-term day count
and the exemption come from the owner's config; when one is unset the figure is None with a
reason, never zero. Tax per lot is on the positive gain only (losses are not set off, which
overstates rather than understates). The exemption, when set, is applied once per scenario with
the stated assumption that no other long-term gains are realised in the year. Every figure is an
estimate from the owner's config and is not tax advice.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from nivesh_engine.returns import holding_days, is_long_term

ZERO, HUNDRED, CENT = Decimal(0), Decimal(100), Decimal("0.01")
EXEMPTION_ONCE = "exemption applied once to this scenario; assumes no other long-term gains are realised this year"  # noqa: E501
NO_COST = "gain unavailable (cost unknown)"


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class TaxLot:
    acquired_on: date
    quantity: Decimal
    cost_per_unit: Decimal | None  # None: cost unknown
    source: str


@dataclass(frozen=True)
class TaxRule:
    long_term_days: int | None = None
    short_rate_pct: Decimal | None = None
    long_rate_pct: Decimal | None = None
    exemption_inr: Decimal | None = None


@dataclass(frozen=True)
class LotTaxLine:
    acquired_on: date
    quantity: Decimal
    holding_days: int
    long_term: bool | None
    days_to_long_term: int | None  # 0 once long-term
    gain: Decimal | None
    tax_now: Decimal | None  # before any exemption
    tax_at_long_term: Decimal | None  # same price assumed, held to eligibility
    saving: Decimal | None
    reason: str | None


@dataclass(frozen=True)
class LotTaxReport:
    lots: tuple[LotTaxLine, ...]
    total_gain: Decimal | None
    total_tax_now: Decimal | None
    total_tax_at_long_term: Decimal | None
    total_saving: Decimal | None
    exemption_inr: Decimal | None
    assumptions: tuple[str, ...]
    reason: str | None


def _rate_tax(gain: Decimal, rate: Decimal) -> Decimal:
    return _q(max(gain, ZERO) * rate / HUNDRED)


def _line(lot: TaxLot, price: Decimal, as_of: date, rule: TaxRule) -> LotTaxLine:
    days = holding_days(lot.acquired_on, as_of)
    term = is_long_term(days, rule.long_term_days)
    to_long = None if rule.long_term_days is None else max(rule.long_term_days - days, 0)
    gain = None if lot.cost_per_unit is None else _q((price - lot.cost_per_unit) * lot.quantity)
    now: Decimal | None = None
    later: Decimal | None = None
    if gain is None:
        why: str | None = NO_COST
    elif term is None:
        why = "long-term threshold not set"
    else:
        rate = rule.long_rate_pct if term else rule.short_rate_pct
        why = None if rate is not None else f"{'long' if term else 'short'}-term rate not set"
        now = None if rate is None else _rate_tax(gain, rate)
        later = None if rule.long_rate_pct is None else _rate_tax(gain, rule.long_rate_pct)
    saving = None if now is None or later is None else now - later
    return LotTaxLine(
        lot.acquired_on, lot.quantity, days, term, to_long, gain, now, later, saving, why
    )


def _scenario_tax(gains: Sequence[tuple[Decimal, bool]], rule: TaxRule) -> Decimal | None:
    """Short-term gains at the short rate; long-term gains pooled, less the exemption once."""
    short = [g for g, lt in gains if not lt]
    long_gain = sum((max(g, ZERO) for g, lt in gains if lt), ZERO)
    if (short and rule.short_rate_pct is None) or (long_gain > 0 and rule.long_rate_pct is None):
        return None
    tax = sum((_rate_tax(g, rule.short_rate_pct or ZERO) for g in short), ZERO)
    if long_gain > 0 and rule.long_rate_pct is not None:
        taxable = max(long_gain - (rule.exemption_inr or ZERO), ZERO)
        tax += _rate_tax(taxable, rule.long_rate_pct)
    return tax


def tax_lots(lots: Sequence[TaxLot], price: Decimal, as_of: date, rule: TaxRule) -> LotTaxReport:
    """Holding days, long-term status, days to long-term, gain and tax now vs at long-term."""
    lines = tuple(_line(x, price, as_of, rule) for x in lots)
    if not lines:
        return LotTaxReport((), None, None, None, None, None, (), "no dated lots")
    gains = [x.gain for x in lines]
    total_gain = None if any(g is None for g in gains) else sum((g or ZERO for g in gains), ZERO)
    reason = next((x.reason for x in lines if x.reason), None)
    now = later = None
    if reason is None:
        now = _scenario_tax([(x.gain or ZERO, bool(x.long_term)) for x in lines], rule)
        later = _scenario_tax([(x.gain or ZERO, True) for x in lines], rule)
    elif total_gain is not None and rule.long_rate_pct is not None:
        later = _scenario_tax([(g or ZERO, True) for g in gains], rule)
    saving = None if now is None or later is None else now - later
    assumptions = (EXEMPTION_ONCE,) if rule.exemption_inr else ()
    return LotTaxReport(
        lines, total_gain, now, later, saving, rule.exemption_inr, assumptions, reason
    )


@dataclass(frozen=True)
class LotPick:
    picks: tuple[tuple[date, Decimal], ...]  # (acquired_on, quantity taken)
    gain: Decimal | None
    tax: Decimal | None
    fifo_note: str | None
    reason: str | None


def _ranked(lots: Sequence[TaxLot], price: Decimal, as_of: date, rule: TaxRule) -> list[TaxLot]:
    def key(x: TaxLot) -> tuple[int, int, Decimal, date]:
        if x.cost_per_unit is None:
            return (2, 0, ZERO, x.acquired_on)
        per_unit = price - x.cost_per_unit
        term = is_long_term(holding_days(x.acquired_on, as_of), rule.long_term_days)
        rate = rule.long_rate_pct if term else rule.short_rate_pct
        loss = 0 if per_unit < 0 else 1
        if term is None or rate is None:
            return (loss, 0 if term else 1, per_unit, x.acquired_on)
        return (loss, 0, max(per_unit, ZERO) * rate, x.acquired_on)

    return sorted(lots, key=key)


def _take(ordered: Sequence[TaxLot], quantity: Decimal) -> list[TaxLot]:
    out: list[TaxLot] = []
    left = quantity
    for x in ordered:
        if left <= 0:
            break
        n = min(x.quantity, left)
        out.append(TaxLot(x.acquired_on, n, x.cost_per_unit, x.source))
        left -= n
    return out


def _rates_known(lots: Sequence[TaxLot], as_of: date, rule: TaxRule) -> bool:
    for x in lots:
        term = is_long_term(holding_days(x.acquired_on, as_of), rule.long_term_days)
        if term is None or (rule.long_rate_pct if term else rule.short_rate_pct) is None:
            return False
    return True


def cheapest_lots(
    lots: Sequence[TaxLot],
    quantity: Decimal,
    price: Decimal,
    as_of: date,
    rule: TaxRule,
    *,
    fifo: bool,
) -> LotPick:
    """Lots for a partial reduction ranked by estimated tax: losses first, then the lowest tax
    per unit, then the oldest. With `fifo` the oldest units go first and a note states the tax
    difference against the ranked choice (informational)."""
    open_qty = sum((x.quantity for x in lots), ZERO)
    if quantity <= 0:
        return LotPick((), None, None, None, "quantity must be positive")
    if quantity > open_qty:
        return LotPick(
            (), None, None, None, f"quantity {quantity} exceeds the open quantity {open_qty}"
        )
    ranked = _take(_ranked(lots, price, as_of, rule), quantity)
    chosen = _take(sorted(lots, key=lambda x: x.acquired_on), quantity) if fifo else ranked
    report = tax_lots(chosen, price, as_of, rule)
    reason = report.reason
    if reason is None and not _rates_known(lots, as_of, rule):
        reason = "rate not set: ranked by long-term first, then gain per unit"
    elif reason is not None and reason != NO_COST:
        reason = f"{reason}: ranked by long-term first, then gain per unit"
    note = None
    picks = tuple((x.acquired_on, x.quantity) for x in chosen)
    if fifo and picks != tuple((x.acquired_on, x.quantity) for x in ranked):
        best = tax_lots(ranked, price, as_of, rule).total_tax_now
        tax = report.total_tax_now
        if tax is None or best is None:
            diff = "tax difference unavailable (rate or cost not set)"
        else:
            diff = f"estimated tax {tax} vs {best} for the lowest-tax lots, difference {tax - best}"
        note = f"FIFO assumed for this holding (oldest units first); {diff}"
    return LotPick(picks, report.total_gain, report.total_tax_now, note, reason)
