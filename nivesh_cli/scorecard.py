"""`nivesh decide`, `rescore`, `scorecard` and `calibration` (ST-12.1 to ST-12.4). Thin: the
logic lives in `nivesh_adapters.score_service` and the `nivesh_engine` scoring modules. None of
these commands calls a model.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from nivesh_adapters.report import save_report, to_json, to_markdown
from nivesh_adapters.report_check import finalize
from nivesh_adapters.report_templates import scorecard_report
from nivesh_adapters.score_service import (
    BASIS_NOTE,
    calibration_for,
    load_scored_calls,
    score_calls,
    scorecard_input,
)
from nivesh_adapters.stores import open_read_stores
from nivesh_cli.common import plain_run, settings_of, user_errors
from nivesh_cli.deliver import DeliverFlag, after_save
from nivesh_cli.engine import AsJson, _as_of
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.ledger import record_decision
from nivesh_core.timeutil import utcnow

scorecard_app = typer.Typer(help="Owner decisions, scoring, the scorecard and calibration.")


def _number(text: str | None, what: str) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise NiveshError(f"{what} must be a number, got {text!r}") from None


def _day(text: str | None, what: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise NiveshError(f"{what} must be an ISO date YYYY-MM-DD, got {text!r}") from None


@scorecard_app.command("decide")
def decide_cmd(
    ctx: typer.Context,
    entry_id: Annotated[int, typer.Argument(help="Ledger entry id of the call.")],
    decision: Annotated[str, typer.Argument(help="acted, skipped or partial.")],
    qty: Annotated[str | None, typer.Option(help="Quantity traded (acted, partial).")] = None,
    price: Annotated[str | None, typer.Option(help="Price per unit (acted, partial).")] = None,
    currency: Annotated[
        str | None, typer.Option(help="INR or USD; default from the market.")
    ] = None,
    note: Annotated[str | None, typer.Option(help="A short note; no personal data.")] = None,
    on: Annotated[str | None, typer.Option("--on", help="Trade date YYYY-MM-DD.")] = None,
    corrects: Annotated[int | None, typer.Option(help="Decision id this row replaces.")] = None,
) -> None:
    """Record what you did about a call. It adds a row; earlier rows are never edited."""
    settings = settings_of(ctx)
    with user_errors():
        data_dir = Path(settings.data_dir)
        init_stores(data_dir)
        sql = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            rid = record_decision(
                sql, entry_id, decision, qty=_number(qty, "--qty"), price=_number(price, "--price"),
                currency=currency, note=note, on_date=_day(on, "--on"), corrects_id=corrects,
                now=utcnow(),
            )  # fmt: skip
            row = sql.execute(
                "SELECT decision, qty, price, currency, on_date FROM ledger_decision WHERE id = ?",
                (rid,),
            ).fetchone()
        finally:
            sql.close()
    sized = "" if row[1] is None else f" qty {row[1]} price {row[2]} {row[3]}"
    fix = "" if corrects is None else f" (replaces decision {corrects})"
    typer.echo(f"decision {rid} recorded: {row[0]} on entry {entry_id}{sized} on {row[4]}{fix}")


@scorecard_app.command("rescore")
def rescore_cmd(ctx: typer.Context, as_json: AsJson = False) -> None:
    """Score every ledger call that has become measurable and add the rows; nothing is edited."""
    settings = settings_of(ctx)
    with user_errors(), open_read_stores(settings, writable_sql=True) as st:
        summary = score_calls(st.sql, st.duck, settings, _as_of(None))  # own read-write sqlite
    if as_json:
        typer.echo(json.dumps({**asdict(summary), "basis": BASIS_NOTE}, sort_keys=True))
        return
    if summary.calls == 0:
        typer.echo("nothing to score: the ledger has no calls yet")
        return
    typer.echo(
        f"scored {summary.scored}, insufficient {summary.insufficient}, pending {summary.pending}"
        f" (from {summary.calls} calls)"
    )
    for n in summary.notes:
        typer.echo(f"note: {n}")
    typer.echo(f"basis: {BASIS_NOTE}")


@contextmanager
def _ledger(ctx: typer.Context) -> Iterator[sqlite3.Connection]:
    """The transactional store, opened for reading only."""
    data_dir = Path(settings_of(ctx).data_dir)
    init_stores(data_dir)
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    sql.execute("PRAGMA query_only = ON")
    try:
        yield sql
    finally:
        sql.close()


@scorecard_app.command("scorecard")
def scorecard_cmd(
    ctx: typer.Context,
    since: Annotated[
        str | None, typer.Option("--since", help="Only calls issued from YYYY-MM-DD.")
    ] = None,
    as_json: AsJson = False,
    deliver: DeliverFlag = False,
) -> None:
    """Hit rate, count, mean and median excess and the worst call by verdict, horizon, market,
    conviction and command, and system calls against calls you acted on."""
    settings = settings_of(ctx)
    with user_errors():
        start = _day(since, "--since")
        with _ledger(ctx) as sql:
            day = _as_of(None)
            data = load_scored_calls(sql, settings, start)
        inp = scorecard_input(data, settings, day, utcnow())
        with plain_run(settings, "scorecard") as m:
            m.status = "ok"
            report = finalize(
                scorecard_report(replace(inp, run_id=m.run_id)), {"as_of": day}, m.run_dir,
                max_digits=settings.report.max_tolerance_digits, metered=m,
            )  # fmt: skip
            save_report(m.run_dir, report, {"as_of": day}, m.tracer)
            after_save(settings, m, deliver)
    if report.banner:
        typer.echo(f"warning: {report.banner}; see {m.run_dir}/report.md", err=True)
    typer.echo(to_json(report) if as_json else to_markdown(report))
    typer.echo(f"saved: {m.run_dir}", err=True)


def _ci(ci: tuple[Decimal, Decimal] | None, suffix: str = "") -> str:
    return "n/a" if ci is None else f"{ci[0]}{suffix} to {ci[1]}{suffix}"


@scorecard_app.command("calibration")
def calibration_cmd(ctx: typer.Context, as_json: AsJson = False) -> None:
    """Does higher conviction hit more often? Hit rate and mean excess per conviction bucket
    with 90 percent bootstrap intervals; needs enough scored calls."""
    settings = settings_of(ctx)
    sc = settings.scoring
    with user_errors(), _ledger(ctx) as sql:
        cal = calibration_for(load_scored_calls(sql, settings), settings)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    **asdict(cal),
                    "basis": BASIS_NOTE,
                    "primary_horizon_weeks": sc.primary_horizon_weeks,
                },
                sort_keys=True,
                default=str,
            )
        )
        return
    if not cal.enough:
        typer.echo(f"not enough data ({cal.n} of {cal.calibration_min} scored calls)")
        typer.echo(f"basis: {BASIS_NOTE}")
        return
    typer.echo(f"calibration at {sc.primary_horizon_weeks} weeks, {cal.n} scored calls")
    for b in cal.buckets:
        if b.n == 0:
            typer.echo(f"{b.conviction:<7} 0 calls")
            continue
        typer.echo(
            f"{b.conviction:<7} {b.n} calls  hit rate {b.hit_pct}% (90% interval "
            f"{_ci(b.hit_ci, '%')})  mean excess {b.mean_excess} (90% interval {_ci(b.excess_ci)})"
        )
    typer.echo(f"verdict: {cal.verdict}")
    typer.echo(f"basis: {BASIS_NOTE}")
