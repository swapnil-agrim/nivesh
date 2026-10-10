"""Consolidation, de-duplication and reconciliation of holdings (ST-2.8, OBJ-1).

Deterministic and pure: Decimal only, no I/O, no clock, no randomness (NFR-9).

Rule: key = ISIN (or `symbol:exchange` when a row has none; `symbol:CURRENCY` for a non-INR row, so
one ticker from different sources dedupes whatever exchange label each carried). Rows of one source
and key are summed.
Across sources the highest-precedence source (InvestRight > depository CAS > RTA CAS > CSV) supplies
quantity, price and value; cost comes from the highest-precedence source that has one. If
`scope_ref` (the holder_ref of the demat linked to InvestRight) is given, depository rows of other
demats are additional holdings, not duplicates.

Non-INR rows are converted with the `FxResult` passed in (valuation-date rate). Without a rate their
INR value stays None ("unavailable"): they are excluded from INR totals and weights, never zero.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from nivesh_core.holdings import PRECEDENCE, Holding, PriceBasis, Source
from nivesh_engine.fx import FxResult, convert_holdings

ZERO = Decimal(0)


class Row(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    isin: str | None
    symbol: str
    exchange: str
    name: str | None
    asset_class: str
    quantity: Decimal
    avg_cost: Decimal | None
    price: Decimal
    price_basis: PriceBasis
    currency: str
    value_native: Decimal | None
    value_inr: Decimal | None
    weight: Decimal | None
    as_of: date
    source: Source
    sources: list[Source]
    unresolved: bool


class SourceCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Source
    holdings: int
    value_inr: Decimal
    share: Decimal


class ReconItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    isin: str
    investright_quantity: Decimal
    cas_quantity: Decimal
    investright_as_of: date
    cas_as_of: date


class Exposure(BaseModel):
    """Share of the portfolio held in one currency; `pct` (0-100) is None when not computable."""

    model_config = ConfigDict(frozen=True)

    currency: str
    value_inr: Decimal | None
    value_native: Decimal | None
    pct: Decimal | None
    available: bool


class Consolidated(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: list[Row]
    total_value: Decimal
    invested: Decimal
    pnl: Decimal
    cost_coverage: Decimal
    coverage: list[SourceCoverage]
    reconciliation: list[ReconItem]
    notes: list[str]
    as_of: date | None
    fx: FxResult | None = None
    exposure: list[Exposure] = []


def _key(h: Holding) -> str:
    if h.isin:
        return h.isin
    return f"{h.symbol}:{h.exchange}" if h.currency == "INR" else f"{h.symbol}:{h.currency}"


def _merged_price(
    rows: Sequence[Holding], qty: Decimal, value: Decimal | None, first: Holding
) -> Decimal:
    """INR rows: value / quantity (as E2). Other currencies keep the native price."""
    if not qty:
        return first.price
    if first.currency == "INR" and value is not None:
        return value / qty
    return sum((r.quantity * r.price for r in rows), ZERO) / qty


def _merge(rows: Sequence[Holding]) -> Holding:
    """Sum rows of one key: quantity added, cost quantity-weighted over rows that have cost."""
    if len(rows) == 1:
        return rows[0]
    qty = sum((r.quantity for r in rows), ZERO)
    known = [r.value_inr for r in rows if r.value_inr is not None]
    value = sum(known, ZERO) if len(known) == len(rows) else None
    costed = [r for r in rows if r.avg_cost is not None]
    cost_qty = sum((r.quantity for r in costed), ZERO)
    cost = (
        sum((r.quantity * (r.avg_cost or ZERO) for r in costed), ZERO) / cost_qty
        if cost_qty
        else None
    )
    first = rows[0]
    refs = {r.holder_ref for r in rows}
    return first.model_copy(
        update={
            "quantity": qty,
            "value_inr": value,
            "price": _merged_price(rows, qty, value, first),
            "avg_cost": cost,
            "as_of": max(r.as_of for r in rows),
            "holder_ref": first.holder_ref if len(refs) == 1 else "",
        }
    )


def _rank(source: Source) -> int:
    return PRECEDENCE.index(source)


def _consolidate_rows(
    holdings: Sequence[Holding], scope_ref: str | None
) -> list[tuple[str, Holding, list[Source]]]:
    main: dict[str, dict[Source, list[Holding]]] = defaultdict(lambda: defaultdict(list))
    extra: dict[str, list[Holding]] = defaultdict(list)
    for h in holdings:
        if scope_ref is not None and h.source == "cas_demat" and h.holder_ref != scope_ref:
            extra[_key(h)].append(h)
        else:
            main[_key(h)][h.source].append(h)
    out: list[tuple[str, Holding, list[Source]]] = []
    for key in sorted(set(main) | set(extra)):
        groups = {s: _merge(rows) for s, rows in main.get(key, {}).items()}
        ranked = sorted(groups, key=_rank)
        extras = extra.get(key, [])
        if ranked:
            chosen = groups[ranked[0]]
            if extras:
                chosen = _merge([chosen, *extras])
            if chosen.avg_cost is None:
                chosen = chosen.model_copy(
                    update={
                        "avg_cost": next(
                            (groups[s].avg_cost for s in ranked if groups[s].avg_cost is not None),
                            None,
                        )
                    }
                )
            sources = ranked + (["cas_demat"] if extras and "cas_demat" not in ranked else [])
        else:
            chosen, sources = _merge(extras), ["cas_demat"]
        out.append((key, chosen, sources))
    return out


def reconcile(holdings: Sequence[Holding], scope_ref: str | None = None) -> list[ReconItem]:
    """InvestRight vs the latest depository CAS (scoped to one demat when `scope_ref` is set)."""
    ir = [h for h in holdings if h.source == "investright" and h.isin]
    cas = [
        h
        for h in holdings
        if h.source == "cas_demat" and h.isin and (scope_ref is None or h.holder_ref == scope_ref)
    ]
    if not ir or not cas:
        return []
    ir_date, cas_date = max(h.as_of for h in ir), max(h.as_of for h in cas)
    ir_q: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cas_q: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for h in ir:
        ir_q[h.isin or ""] += h.quantity
    for h in cas:
        cas_q[h.isin or ""] += h.quantity
    return [
        ReconItem(
            isin=isin,
            investright_quantity=ir_q.get(isin, ZERO),
            cas_quantity=cas_q.get(isin, ZERO),
            investright_as_of=ir_date,
            cas_as_of=cas_date,
        )
        for isin in sorted(set(ir_q) | set(cas_q))
        if ir_q.get(isin, ZERO) != cas_q.get(isin, ZERO)
    ]


def _native(h: Holding) -> Decimal | None:
    return h.value_native if h.currency != "INR" else None


def _order(t: tuple[str, Holding, list[Source]]) -> tuple[int, Decimal, str]:
    v = t[1].value_inr
    return (0, -v, t[0]) if v is not None else (1, ZERO, t[0])


def _exposure(rows: list[Row], total: Decimal) -> list[Exposure]:
    foreign = [r for r in rows if r.currency != "INR"]
    if not foreign:
        return []
    excluded = any(r.value_inr is None for r in foreign)
    out: list[Exposure] = []
    for cur in ["INR", *sorted({r.currency for r in foreign})]:
        part = [r for r in rows if r.currency == cur]
        val = sum((r.value_inr for r in part if r.value_inr is not None), ZERO)
        known = all(r.value_inr is not None for r in part)
        native = sum((r.value_native for r in part if r.value_native is not None), ZERO)
        ok = not excluded and known and total > 0
        out.append(
            Exposure(
                currency=cur,
                value_inr=val if known else None,
                value_native=native if cur != "INR" else None,
                pct=val * 100 / total if ok else None,
                available=ok,
            )
        )
    return out


def consolidate(
    holdings: Sequence[Holding], scope_ref: str | None = None, fx: FxResult | None = None
) -> Consolidated:
    if fx is not None:
        holdings = convert_holdings(holdings, fx)
    merged = _consolidate_rows(holdings, scope_ref)
    total = sum((h.value_inr for _, h, _ in merged if h.value_inr is not None), ZERO)
    ordered = sorted(merged, key=_order)
    rows = [
        Row(
            key=k,
            isin=h.isin,
            symbol=h.symbol,
            exchange=h.exchange,
            name=h.name,
            asset_class=h.asset_class,
            quantity=h.quantity,
            avg_cost=h.avg_cost,
            price=h.price,
            price_basis=h.price_basis,
            currency=h.currency,
            value_native=_native(h),
            value_inr=h.value_inr,
            weight=None if h.value_inr is None else (h.value_inr / total if total else ZERO),
            as_of=h.as_of,
            source=h.source,
            sources=srcs,
            unresolved=h.unresolved,
        )  # fmt: skip
        for k, h, srcs in ordered
    ]
    rate = fx.rate if fx is not None else None

    def cost_inr(r: Row) -> Decimal:
        per_unit = r.quantity * (r.avg_cost or ZERO)
        return per_unit if r.currency == "INR" else per_unit * (rate or ZERO)

    costed = [r for r in rows if r.avg_cost is not None and r.value_inr is not None]
    invested = sum((cost_inr(r) for r in costed), ZERO)
    costed_value = sum((r.value_inr or ZERO for r in costed), ZERO)
    coverage = [
        SourceCoverage(
            source=s,
            holdings=sum(1 for r in rows if r.source == s),
            value_inr=(v := sum((r.value_inr or ZERO for r in rows if r.source == s), ZERO)),
            share=v / total if total else ZERO,
        )
        for s in PRECEDENCE
        if any(r.source == s for r in rows)
    ]
    notes: list[str] = []
    book = [r for r in rows if r.price_basis == "avg_cost"]
    if book:
        notes.append(
            f"{len(book)} CSV holding(s) are valued at average cost (book value); "
            "there is no market price for them"
        )
    sources = {h.source for h in holdings}
    if scope_ref is None and {"investright", "cas_demat"} <= sources:
        notes.append(
            "InvestRight and depository CAS rows are matched by ISIN across all demats; "
            "set investright.demat_ref to scope the overlap to one demat"
        )
    foreign = [r for r in rows if r.currency != "INR"]
    missing = [r for r in foreign if r.value_inr is None]
    if missing:
        why = fx.reason if fx is not None and fx.reason else "no USDINR rate was supplied"
        notes.append(f"{len(missing)} USD holding(s) excluded from INR totals and weights: {why}")
    if any(r.avg_cost is not None and r.value_inr is not None for r in foreign):
        notes.append(
            "USD cost is converted at the valuation-date rate, so INR P&L on USD holdings "
            "excludes FX gain on cost"
        )
    return Consolidated(
        rows=rows,
        total_value=total,
        invested=invested,
        pnl=costed_value - invested,
        cost_coverage=costed_value / total if total else ZERO,
        coverage=coverage,
        reconciliation=reconcile(holdings, scope_ref),
        notes=notes,
        as_of=max((h.as_of for h in holdings), default=None),
        fx=fx if foreign else None,
        exposure=_exposure(rows, total),
    )
