"""Glue for portfolio review (ST-8.2 to ST-8.4): stored holdings to engine inputs.

Read-only and offline: it reads the SQLite store and never fetches or writes. Tax inputs follow
one precedence per asset, never merged: Indian equity and ETF lots use `tax.india`, mutual-fund
lots use `mf.tax`, and US lots use `tax.us_long_term_days` for the long-term flag only (US tax is
not modelled). `Profile.tax_rates` is legacy and never read.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb

from nivesh_adapters.analysis_data import load_screen_inputs, load_xray_inputs
from nivesh_adapters.analysis_service import xray_report
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.holdings import Holding
from nivesh_core.holdings_store import latest_holdings, latest_lots, list_transactions
from nivesh_core.profile import Profile
from nivesh_core.review_config import ReviewSettings
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_engine.consolidate import Row
from nivesh_engine.metrics import (
    REGISTRY,
    Bundles,
    evaluate,
    inputs_needed,
    metric_values,
    value_of,
)
from nivesh_engine.mf_lots import fifo_lots
from nivesh_engine.rebalance import Position
from nivesh_engine.review_rules import ReviewFacts, TriggerFacts
from nivesh_engine.statements import StatementRow, latest_as_of
from nivesh_engine.tax_lots import TaxLot, TaxRule, cheapest_lots, tax_lots
from nivesh_engine.xray import UNCLASSIFIED, Xray, _mf_class

TAX_LABEL = "estimate from your config; not tax advice"
INDIA_NO_LOTS = "holding period unavailable: no dated lots"
US_UNMODELLED = "US tax not modelled"


@dataclass(frozen=True)
class LotBasis:
    lots: tuple[TaxLot, ...]
    rule: TaxRule
    fifo: bool  # oldest units go first (mutual-fund folios)
    price: Decimal
    currency: str
    reason: str | None
    notes: tuple[str, ...] = ()


def _security_id(sql: sqlite3.Connection, h: Holding | Row) -> int | None:
    row = None
    if h.isin:
        row = sql.execute(
            "SELECT id FROM security WHERE isin = ? ORDER BY unresolved, id LIMIT 1", (h.isin,)
        ).fetchone()
    if row is None:
        row = sql.execute(
            "SELECT id FROM security WHERE symbol = ? AND exchange = ?", (h.symbol, h.exchange)
        ).fetchone()
    return None if row is None else int(row[0])


def holdings_of(sql: sqlite3.Connection, security_id: int) -> list[Holding]:
    """Latest holding rows of one security (one per account and holder)."""
    return [h for h in latest_holdings(sql) if _security_id(sql, h) == security_id]


def lot_basis(sql: sqlite3.Connection, holding: Holding | Row, settings: Settings) -> LotBasis:
    """Dated lots, the tax rule and the FIFO flag for one holding."""
    if holding.currency == "USD":
        lots = tuple(
            TaxLot(x.acquired_on, x.quantity, x.cost_per_unit, x.source)
            for x in latest_lots(sql)
            if (x.symbol, x.exchange) == (holding.symbol, holding.exchange)
        )
        rule = TaxRule(long_term_days=settings.tax.us_long_term_days)
        return LotBasis(lots, rule, False, holding.price, "USD", US_UNMODELLED)
    if holding.asset_class == "mf":
        mf = settings.mf.tax
        rule = TaxRule(mf.long_term_days, mf.short_rate_pct, mf.long_rate_pct, None)
        if not holding.isin:
            return LotBasis((), rule, True, holding.price, "INR", "no ISIN to match transactions")
        # ponytail: FIFO across every folio of the ISIN; per-folio FIFO when folios are stored
        report = fifo_lots(list_transactions(sql, isin=holding.isin))
        lots = tuple(TaxLot(x.acquired_on, x.units, x.cost_per_unit, "cas") for x in report.lots)
        reason = report.data_error or (None if lots else "no open lots")
        notes = tuple(report.warnings)
        return LotBasis(lots, rule, True, holding.price, "INR", reason, notes)
    t = settings.tax.india
    rule = TaxRule(t.long_term_days, t.short_rate_pct, t.long_rate_pct, t.ltcg_exemption_inr)
    return LotBasis((), rule, False, holding.price, holding.currency, INDIA_NO_LOTS)


# ---- review facts (ST-8.2) ------------------------------------------------------------------
RS_METRIC = (
    "rs_benchmark_change_6m_pct"  # ponytail: fixed 6m window name; follow ta windows if they change
)
QTY = Decimal("0.0001")
PCT = Decimal("0.01")


def quarterly_series(
    rows: Sequence[StatementRow], as_of: date
) -> tuple[tuple[Decimal | None, ...] | None, tuple[Decimal | None, ...] | None]:
    """Quarterly revenue growth (vs the same quarter a year earlier) and operating margin, both
    in percent and oldest first, from the filings known on `as_of`; None without quarters."""
    seen = latest_as_of([r for r in rows if r.period_type == "Q"], as_of)
    rev = {r.period_end: r.value for r in seen if r.item == "revenue"}
    op = {r.period_end: r.value for r in seen if r.item == "operating_income"}
    if not rev:
        return None, None
    ends = sorted(rev)
    growth: list[Decimal | None] = []
    for e in ends:
        prior = next((rev[p] for p in ends if 350 <= (e - p).days <= 380), None)
        growth.append(
            None if not prior or prior <= 0 else ((rev[e] / prior - 1) * 100).quantize(PCT)
        )
    margin = tuple(
        None if e not in op or rev[e] <= 0 else (op[e] / rev[e] * 100).quantize(PCT) for e in ends
    )
    return tuple(growth), (margin if op else None)


def trigger_facts(
    b: Bundles, weights: tuple[Decimal | None, Decimal | None], profile: Profile,
    cfg: ReviewSettings,
) -> TriggerFacts:  # fmt: skip
    """The section 15.6 rule inputs of one security (criteria are merged later)."""
    growth, margin = quarterly_series(b.inp.rows, b.as_of)
    closes = tuple(x.close for x in b.inp.bars if x.date <= b.as_of)
    v = b.valuation()
    mr = v.multiples.get(cfg.valuation_metric) if v is not None else None
    pct = mr.percentiles.get(f"{cfg.valuation_history_years}y") if mr is not None else None
    return TriggerFacts(
        criteria=None, revenue_growth=growth, operating_margin=margin,
        valuation_percentile=None if pct is None else value_of(pct),
        revisions=value_of(evaluate("revisions", b)),
        position_weight_pct=weights[0], sector_weight_pct=weights[1],
        max_position_pct=Decimal(str(profile.max_position_pct)),
        max_sector_pct=Decimal(str(profile.max_sector_pct)),
        closes=closes or None, rs_change=value_of(evaluate(RS_METRIC, b)),
    )  # fmt: skip


def security_weights(xray: Xray, sec: SecurityRow) -> tuple[Decimal | None, Decimal | None]:
    """Position and sector weight (percent of the valued portfolio) of one security."""
    keys = {sec.isin, f"{sec.symbol}:{sec.exchange}", f"{sec.symbol}:{sec.currency}"}
    found = [p.weight_pct for p in xray.positions if p.key in keys]
    sectors = {b.name: b.weight_pct for b in xray.allocation.get("sector", ())}
    return (
        sum(found, Decimal(0)) if found else None,
        sectors.get(sec.sector) if sec.sector else None,
    )


def lot_notes(basis: LotBasis, as_of: date, *, trim_quantity: Decimal | None) -> tuple[str, str]:
    """(tax note, lowest-tax trim note) from the holding's lots, labelled as estimates. The trim
    note covers `trim_quantity` units, or the whole open quantity ranked when None."""
    if not basis.lots:
        return f"{TAX_LABEL}: {basis.reason or 'no dated lots'}", ""
    r = tax_lots(basis.lots, basis.price, as_of, basis.rule)

    def s(x: Decimal | None) -> str:
        return "n/a" if x is None else str(x)

    extra = "; ".join(x for x in (basis.reason, r.reason) if x)
    tax = (
        f"{TAX_LABEL}: gain {s(r.total_gain)}, tax now {s(r.total_tax_now)}, at long-term "
        f"{s(r.total_tax_at_long_term)}, saving {s(r.total_saving)}"
        + (f"; {extra}" if extra else "")
    )
    held = sum((x.quantity for x in basis.lots), Decimal(0))
    qty = held if trim_quantity is None else min(trim_quantity, held)
    why = "whole position ranked" if trim_quantity is None else "excess over max_position_pct"
    pick = cheapest_lots(basis.lots, qty, basis.price, as_of, basis.rule, fifo=basis.fifo)
    lots = ", ".join(f"{d} x {q}" for d, q in pick.picks)
    trim = f"lowest-tax lots for a trim of {qty} units ({why}): {lots}; tax {s(pick.tax)}"
    trim += "".join(f"; {x}" for x in (pick.reason, pick.fifo_note) if x)
    return tax, trim


def review_facts(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, holding: Holding, as_of: date, *, xray: Xray | None = None,
) -> ReviewFacts:  # fmt: skip
    """Metrics, rule inputs and tax notes of one held security, from the stores only. Pass the
    portfolio `xray` when reviewing several holdings so it is computed once."""
    sid = _security_id(sql, holding)
    sec = None if sid is None else SecurityMaster(sql).get(sid)
    if sid is None or sec is None:
        raise NiveshError(f"{holding.symbol} is not in the security master")
    cfg = settings.analysis
    inp = load_screen_inputs(duck, sql, [sid], as_of, cfg, needs=inputs_needed(REGISTRY))[sid]
    b = Bundles(inp, as_of, cfg)
    if xray is None:
        xray = xray_report(duck, sql, settings, profile, as_of)["xray"]
    weights = security_weights(xray, sec)
    triggers = trigger_facts(b, weights, profile, settings.review)
    basis = lot_basis(sql, holding, settings)
    qty = None
    pos, cap = weights[0], triggers.max_position_pct
    if pos is not None and pos > cap and basis.lots:
        held = sum((x.quantity for x in basis.lots), Decimal(0))
        qty = (held * (pos - cap) / pos).quantize(QTY)
    tax, trim = lot_notes(basis, as_of, trim_quantity=qty)
    return ReviewFacts(sid, metric_values(b), triggers, tax, trim)


# ---- rebalance inputs (ST-8.4) --------------------------------------------------------------
TAX_RATE_Q = Decimal("0.0001")


def tax_per_inr(basis: LotBasis, as_of: date) -> Decimal | None:
    """Estimated tax now per rupee of the whole position; None without lots or rates."""
    if not basis.lots:
        return None
    tax = tax_lots(basis.lots, basis.price, as_of, basis.rule).total_tax_now
    value = sum((x.quantity * basis.price for x in basis.lots), Decimal(0))
    return None if tax is None or value <= 0 else (tax / value).quantize(TAX_RATE_Q)


def rebalance_inputs(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, as_of: date,
    flags: Mapping[int, str],
) -> tuple[tuple[Position, ...], tuple[str, ...]]:  # fmt: skip
    """One position per consolidated holding with an INR value, in its target asset class (the
    X-ray mapping), with the review action of its security from `flags` and its estimated tax
    per rupee. Rows without an INR value are left out with a note."""
    inp = load_xray_inputs(duck, sql, settings, as_of)
    cfg = settings.analysis.xray
    out: list[Position] = []
    notes = list(inp.notes)
    for r in sorted(inp.rows, key=lambda r: r.key):
        if r.value_inr is None:
            notes.append(f"{r.name or r.symbol}: left out (no INR value)")
            continue
        if r.asset_class == "mf":
            cls = _mf_class(inp.mf_category_of.get(r.key), cfg.mf_category_map)
        else:
            cls = cfg.asset_class_map.get(r.asset_class, UNCLASSIFIED)
        sid = _security_id(sql, r)
        flag = None if sid is None else flags.get(sid)
        rate = tax_per_inr(lot_basis(sql, r, settings), as_of)
        out.append(Position(r.key, r.name or r.symbol, cls, r.value_inr, flag, rate))
    return tuple(out), tuple(notes)
