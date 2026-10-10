"""`nivesh review tax | holdings | rebalance` (ST-8.2 to ST-8.4): estimated tax per lot, the
holding review and an asset-class rebalancing proposal.

Every tax figure is an estimate from the owner's config and is not tax advice. `tax` and
`rebalance` fetch, spend and write nothing. `holdings` computes every fact read-only, closes the
stores, and then asks the reviewer agent once per holding with a thesis (a metered run); code
floors each action. `rebalance` only proposes moves.
"""

import asyncio
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from nivesh_adapters.analysis_service import plain, resolve_security, xray_report
from nivesh_adapters.review_service import (
    TAX_LABEL,
    holdings_of,
    lot_basis,
    rebalance_inputs,
    review_facts,
)
from nivesh_agents.analysts import Target
from nivesh_agents.context import RunCtx
from nivesh_agents.holding_review import ReviewItem, review_all
from nivesh_agents.prompts import load_prompt
from nivesh_agents.schemas import HoldingReview
from nivesh_agents.store import RunStore
from nivesh_cli.common import metered_run, profile_of, settings_of, user_errors
from nivesh_cli.engine import AsJson, AsOf, _as_of, reader
from nivesh_cli.thesis import held_securities, store_reader
from nivesh_core.errors import NiveshError
from nivesh_core.thesis_store import active_theses
from nivesh_engine.rebalance import RebalancePlan, propose_moves
from nivesh_engine.tax_lots import LotPick, LotTaxLine, cheapest_lots, tax_lots

review_app = typer.Typer(
    no_args_is_help=True, help="Portfolio review: holdings, tax lots and rebalancing."
)


def _v(x: object) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "yes" if x else "no"
    return str(x)


def _line(x: LotTaxLine) -> str:
    return (
        f"{x.acquired_on}  qty {x.quantity}  days {x.holding_days}  long-term {_v(x.long_term)}"
        f"  days to long-term {_v(x.days_to_long_term)}  gain {_v(x.gain)}  tax now {_v(x.tax_now)}"
        f"  at long-term {_v(x.tax_at_long_term)}  saving {_v(x.saving)}"
        + (f"  ({x.reason})" if x.reason else "")
    )


def _trim(quantity: Decimal, pick: LotPick) -> list[str]:
    if not pick.picks:
        return [f"trim {quantity}: {pick.reason}"]
    lots = ", ".join(f"{d} x {q}" for d, q in pick.picks)
    out = [f"trim {quantity}: {lots}; gain {_v(pick.gain)}; tax {_v(pick.tax)}"]
    out += [f"  {pick.reason}"] if pick.reason else []
    out += [f"  {pick.fifo_note}"] if pick.fifo_note else []
    return out


@review_app.command("tax")
def tax_cmd(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="ISIN, symbol or company name.")],
    trim: Annotated[
        str | None, typer.Option("--trim", help="Quantity to rank lots for (lowest tax first).")
    ] = None,
    as_of: AsOf = None,
    as_json: AsJson = False,
) -> None:
    """Per-lot holding period, long-term status, gain and estimated tax now vs at long-term."""
    with user_errors(), store_reader(ctx) as sql:
        day = _as_of(as_of)
        sec = resolve_security(sql, symbol)
        held = holdings_of(sql, sec.id)
        if not held:
            raise NiveshError(f"{sec.symbol} is not held")
        basis = lot_basis(sql, held[0], settings_of(ctx))
        report = tax_lots(basis.lots, basis.price, day, basis.rule)
        pick = None
        qty = None
        if trim is not None:
            try:
                qty = Decimal(trim)
            except ArithmeticError:
                raise NiveshError(f"--trim must be a number, got {trim!r}") from None
            if not qty.is_finite():
                raise NiveshError(f"--trim must be a finite number, got {trim!r}")
            pick = cheapest_lots(basis.lots, qty, basis.price, day, basis.rule, fifo=basis.fifo)
    if as_json:
        out = {
            "command": "review tax", "subject": sec.symbol, "as_of": day, "label": TAX_LABEL,
            "currency": basis.currency, "reason": basis.reason, "notes": basis.notes,
            "report": report, "trim": pick,
        }  # fmt: skip
        typer.echo(json.dumps(plain(out), sort_keys=True))
        return
    typer.echo(f"review tax {sec.symbol} as of {day} ({basis.currency}; {TAX_LABEL})")
    for line in report.lots:
        typer.echo(_line(line))
    if report.lots:
        typer.echo(
            f"total: gain {_v(report.total_gain)}  tax now {_v(report.total_tax_now)}"
            f"  at long-term {_v(report.total_tax_at_long_term)}  saving {_v(report.total_saving)}"
        )
    for note in (basis.reason, *basis.notes, *report.assumptions):
        if note:
            typer.echo(f"note: {note}")
    if qty is not None and pick is not None:
        for line_text in _trim(qty, pick):
            typer.echo(line_text)


def _review_lines(symbol: str, r: HoldingReview) -> list[str]:
    out = [f"{symbol}  {r.action}  {r.confidence}  thesis {r.thesis_status}"]
    out += [f"  reason {x.code}: {x.text}" for x in r.reasons]
    out += [f"  criterion {c.criterion_id}: {c.status} ({c.judged_by})" for c in r.criteria]
    out += [f"  {t.code}: {t.status} ({t.detail})" for t in r.triggers]
    out.append(f"  tax: {r.tax_note}")
    out += [f"  overrides: {'; '.join(r.overrides)}"] if r.overrides else []
    return out


@review_app.command("holdings")
def holdings_cmd(
    ctx: typer.Context,
    only: Annotated[str | None, typer.Option("--only", help="One holding: ISIN or symbol.")] = None,
    as_json: AsJson = False,
) -> None:
    """HOLD, ADD, TRIM, EXIT or REVIEW per equity holding with a thesis, with reasons."""
    settings, profile = settings_of(ctx), profile_of(ctx)
    with user_errors(), reader(ctx) as rd:
        day = _as_of(None)
        held = held_securities(rd.sql)
        if only is not None:
            sid = resolve_security(rd.sql, only).id
            held = [h for h in held if h.security_id == sid]
        theses = active_theses(rd.sql)
        missing = [h.symbol for h in held if h.security_id not in theses]
        xray = None
        items: list[ReviewItem] = []
        for h in held:
            if h.security_id not in theses:
                continue
            if xray is None:
                xray = xray_report(rd.duck, rd.sql, settings, profile, day)["xray"]
            holding = holdings_of(rd.sql, h.security_id)[0]
            facts = review_facts(rd.duck, rd.sql, settings, profile, holding, day, xray=xray)
            target = Target(h.security_id, h.symbol, h.name or h.symbol, h.asset_class, h.market)
            items.append(ReviewItem(target, theses[h.security_id], facts))
    reviews: list[HoldingReview] = []
    if items:  # the stores are closed before any agent call (the engine server opens them)
        cfg = settings.agents
        with metered_run(settings, profile, "review holdings", "quick", force=False) as m:
            version = load_prompt("holding_review", pins=cfg.prompt_pins).version
            m.tracer.start("review holdings", f"holding_review:v{version}")
            run = RunCtx(cfg, m.tracer, day, "quick", settings.prices, settings.usd_inr, m.env)
            store = RunStore(m.run_dir, m.tracer)
            reviews = asyncio.run(review_all(run, items, settings.review, store))
            m.tracer.finish_run(getattr(cfg.models, cfg.tiers["holding_review"]))
            m.status = "ok"
    if as_json:
        out = {
            "command": "review holdings", "as_of": day.isoformat(), "without_thesis": missing,
            "reviews": [r.model_dump(mode="json") for r in reviews],
        }  # fmt: skip
        typer.echo(json.dumps(out, sort_keys=True))
        return
    typer.echo(f"review holdings as of {day}")
    for it, r in zip(items, reviews, strict=True):
        for line in _review_lines(it.target.symbol, r):
            typer.echo(line)
    if not items:
        typer.echo("nothing to review: no equity or ETF holding with an active thesis")
    if missing:
        typer.echo(f"without a thesis: {', '.join(missing)} (run `nivesh thesis onboard`)")


def review_flags(data_dir: Path, run_id: int) -> tuple[dict[int, str], list[str]]:
    """The action per security from the holding reviews saved by `review holdings` run
    `run_id`. Failed markers and files that do not validate are skipped with a note."""
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise NiveshError("--review-run must be a positive run number")
    outputs = data_dir / "runs" / str(run_id) / "outputs"  # built from the integer only
    files = sorted(outputs.glob("*_holding_review_*.json")) if outputs.is_dir() else []
    if not files:
        raise NiveshError(f"run {run_id} has no saved holding reviews")
    flags: dict[int, str] = {}
    notes: list[str] = []
    for f in files:
        try:
            r = HoldingReview.model_validate_json(f.read_bytes())
        except ValueError:
            notes.append(f"{f.name}: skipped (not a valid holding review)")
            continue
        flags[r.security_id] = r.action
    return flags, notes


def _plan_lines(plan: RebalancePlan, notes: list[str]) -> list[str]:
    if plan.reason:
        return [f"no proposal: {plan.reason}", *(f"note: {n}" for n in notes)]
    out = [
        f"  {m.kind:<6} {m.asset_class:<12} {m.name or '-':<24} {m.amount_inr}  ({m.basis})"
        for m in plan.moves
    ] or ["  no moves: every asset class is inside its band"]
    out.append("pro-forma (before -> after vs target, percent):")
    out += [
        f"  {x.asset_class:<12} {x.before_pct} -> {x.after_pct} vs {x.target_pct}"
        + ("" if x.within_band else "  out of band")
        for x in plan.pro_forma
    ]
    out.append(f"turnover: {plan.turnover_pct}% of the portfolio")
    return out + [f"note: {n}" for n in (*plan.notes, *notes)]


@review_app.command("rebalance")
def rebalance_cmd(
    ctx: typer.Context,
    cash: Annotated[str, typer.Option("--cash", help="New cash to add, in INR.")] = "0",
    review_run: Annotated[
        int | None,
        typer.Option("--review-run", help="Use the EXIT/TRIM actions of this review run."),
    ] = None,
    as_json: AsJson = False,
) -> None:
    """Asset-class moves back inside the band: new cash, then reviewed holdings, then lowest
    tax; with pro-forma weights and turnover. A proposal only."""
    settings, profile = settings_of(ctx), profile_of(ctx)
    with user_errors():
        try:
            new_cash = Decimal(cash)
        except InvalidOperation:
            raise NiveshError(f"--cash must be a number, got {cash!r}") from None
        if not new_cash.is_finite() or new_cash < 0:
            raise NiveshError("--cash must be zero or more")
        flags: dict[int, str] = {}
        notes: list[str] = []
        if review_run is not None:
            flags, notes = review_flags(Path(settings.data_dir), review_run)
        else:
            notes.append("no review run given: no holding is flagged EXIT or TRIM")
        with reader(ctx) as rd:
            day = _as_of(None)
            positions, more = rebalance_inputs(rd.duck, rd.sql, settings, day, flags)
        cfg = settings.review
        targets = {k: Decimal(str(v)) for k, v in profile.target_allocation.items()}
        plan = propose_moves(
            targets, positions, band_pp=cfg.band_pp, new_cash=new_cash,
            turnover_limit_pct=cfg.turnover_limit_pct,
        )  # fmt: skip
        notes += more
    if as_json:
        out = {"command": "review rebalance", "as_of": day, "plan": plan, "notes": notes}
        typer.echo(json.dumps(plain(out), sort_keys=True))
        return
    typer.echo(
        f"review rebalance as of {day} (band +-{cfg.band_pp}pp, turnover cap "
        f"{cfg.turnover_limit_pct}% per proposal; a proposal, not advice)"
    )
    for line in _plan_lines(plan, notes):
        typer.echo(line)
