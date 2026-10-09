"""Consolidation, de-duplication and reconciliation of holdings (ST-2.8, OBJ-1).

Deterministic and pure: Decimal only, no I/O, no clock, no randomness (NFR-9).

Rule: key = ISIN (or `symbol:exchange` when a row has none). Rows of one source and key are summed.
Across sources the highest-precedence source (InvestRight > depository CAS > RTA CAS > CSV) supplies
quantity, price and value; cost comes from the highest-precedence source that has one. If
`scope_ref` (the holder_ref of the demat linked to InvestRight) is given, depository rows of other
demats are additional holdings, not duplicates.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from nivesh_core.holdings import PRECEDENCE, Holding, PriceBasis, Source

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
    value_inr: Decimal
    weight: Decimal
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


def _key(h: Holding) -> str:
    return h.isin or f"{h.symbol}:{h.exchange}"


def _merge(rows: Sequence[Holding]) -> Holding:
    """Sum rows of one key: quantity added, cost quantity-weighted over rows that have cost."""
    if len(rows) == 1:
        return rows[0]
    qty = sum((r.quantity for r in rows), ZERO)
    value = sum((r.value_inr for r in rows), ZERO)
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
            "price": value / qty if qty else first.price,
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


def consolidate(holdings: Sequence[Holding], scope_ref: str | None = None) -> Consolidated:
    merged = _consolidate_rows(holdings, scope_ref)
    total = sum((h.value_inr for _, h, _ in merged), ZERO)
    ordered = sorted(merged, key=lambda t: (-t[1].value_inr, t[0]))
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
            value_inr=h.value_inr,
            weight=h.value_inr / total if total else ZERO,
            as_of=h.as_of,
            source=h.source,
            sources=srcs,
            unresolved=h.unresolved,
        )  # fmt: skip
        for k, h, srcs in ordered
    ]
    costed = [r for r in rows if r.avg_cost is not None]
    invested = sum((r.quantity * (r.avg_cost or ZERO) for r in costed), ZERO)
    costed_value = sum((r.value_inr for r in costed), ZERO)
    coverage = [
        SourceCoverage(
            source=s,
            holdings=sum(1 for r in rows if r.source == s),
            value_inr=(v := sum((r.value_inr for r in rows if r.source == s), ZERO)),
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
    )
