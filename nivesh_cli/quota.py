"""`nivesh quota status|set` (ADR-0016): what the local vendor call ledger says is used against the
plan limits, and the two ways to keep it honest. `set` overrides a limit (the plan numbers are the
owner's belief, nothing is read from the vendor) or tops a day up with the calls that another
client of the same plan spent. Only the SQLite store is opened, never the market database, so
both commands work while an ingest holds that one's single writer.
"""

import json
from datetime import date, timedelta
from typing import Annotated, Any

import typer

from nivesh_adapters.stores import open_sql
from nivesh_cli.common import settings_of, user_errors
from nivesh_core.errors import NiveshError
from nivesh_core.fundamentals_config import PlanSettings
from nivesh_core.quota import (
    MAX_CALLS,
    WindowUse,
    add_manual,
    set_limit,
    status_of,
    used_on,
)
from nivesh_core.timeutil import ist_date, utcnow

quota_app = typer.Typer(
    no_args_is_help=True, help="The vendor call ledger: usage against the plan limits."
)
AsJson = Annotated[bool, typer.Option("--json", help="Print the counts as JSON.")]


def _plans(ctx: typer.Context, vendor: str | None) -> dict[str, PlanSettings]:
    """The vendors that have a plan in `fundamentals.providers`, or just `vendor`."""
    plans = {p.name: p.plan for p in settings_of(ctx).fundamentals.providers if p.plan is not None}
    if vendor is not None and vendor not in plans:
        known = ", ".join(plans) or "none"
        raise NiveshError(f"unknown vendor {vendor!r}; vendors with a plan: {known}")
    return {vendor: plans[vendor]} if vendor is not None else plans


def _window(w: WindowUse) -> dict[str, Any]:
    return {
        "starts_on": w.starts_on.isoformat(), "resets_on": w.resets_on.isoformat(),
        "used": w.used, "manual": w.manual, "limit": w.limit, "remaining": w.remaining,
        "limit_source": w.source,
    }  # fmt: skip


def _line(label: str, w: WindowUse) -> str:
    use = f"{w.used:,} used, no cap" if w.limit is None else f"{w.used:,} of {w.limit:,} used"
    left = "" if w.remaining is None else f", {w.remaining:,} left"
    manual = f"; manual calls {w.manual:,}" if w.manual else ""
    return f"  {label}: {use}{left}; limit from {w.source}; resets {w.resets}{manual}"


@quota_app.command("status")
def status_cmd(
    ctx: typer.Context,
    vendor: Annotated[str | None, typer.Option(help="Only this vendor.")] = None,
    as_json: AsJson = False,
) -> None:
    """Used and remaining calls today and this month, per vendor with a plan."""
    with user_errors():
        plans = _plans(ctx, vendor)
        with open_sql(settings_of(ctx)) as sql:
            found = [status_of(sql, name, plan, now=utcnow) for name, plan in plans.items()]
    if as_json:
        body = [{"vendor": s.vendor, "day": _window(s.day), "month": _window(s.month),
                 "warnings": list(s.warnings)} for s in found]  # fmt: skip
        typer.echo(json.dumps({"command": "quota status", "vendors": body}, sort_keys=True))
        return
    if not found:
        typer.echo("no vendor has a plan in fundamentals.providers")
    for s in found:
        typer.echo(s.vendor)
        typer.echo(_line(f"today {s.day.starts_on}", s.day))
        typer.echo(_line(f"month {s.month.starts_on} to {s.month.resets_on - timedelta(days=1)}",
                         s.month))  # fmt: skip
        for warning in s.warnings:
            typer.echo(f"  warning: {warning}")


def _day(on: str | None, today: date) -> date:
    if on is None:
        return today
    try:
        day = date.fromisoformat(on)
    except ValueError:
        raise NiveshError(f"--on must be an IST date such as {today.isoformat()}") from None
    if day > today:
        raise NiveshError("--on cannot be in the future")
    return day


@quota_app.command("set")
def set_cmd(
    ctx: typer.Context,
    vendor: Annotated[str, typer.Argument(help="A vendor with a plan, for example tapetide.")],
    daily: Annotated[int | None, typer.Option(help="Override the daily limit.")] = None,
    monthly: Annotated[int | None, typer.Option(help="Override the monthly limit.")] = None,
    used_today: Annotated[
        int | None,
        typer.Option(
            help="Calls already used elsewhere today: tops the ledger up to this, never down."
        ),
    ] = None,
    on: Annotated[
        str | None, typer.Option(help="IST date (YYYY-MM-DD) for --used-today; default today.")
    ] = None,
) -> None:
    """Override a plan limit, or record calls another client of the same plan has spent."""
    with user_errors():
        today = ist_date(utcnow())
        if daily is None and monthly is None and used_today is None:
            raise NiveshError("give --daily, --monthly or --used-today")
        if on is not None and used_today is None:
            raise NiveshError("--on needs --used-today")
        if used_today is not None and used_today < 1:
            raise NiveshError("--used-today must be 1 or more")
        for name, v in (("--daily", daily), ("--monthly", monthly), ("--used-today", used_today)):
            if v is not None and v > MAX_CALLS:
                raise NiveshError(f"{name} must be at most {MAX_CALLS:,}")
        day = _day(on, today)
        _plans(ctx, vendor)  # an unknown vendor stops here
        with open_sql(settings_of(ctx), writable=True) as sql:
            if daily is not None or monthly is not None:
                set_limit(sql, vendor, daily=daily, monthly=monthly, now=utcnow)
                pairs = (("daily", daily), ("monthly", monthly))
                said = [f"{n} {v:,}" for n, v in pairs if v is not None]
                typer.echo(f"wrote {vendor} limit override: {', '.join(said)}")
            if used_today is not None:
                shown = used_on(sql, vendor, day)
                if shown >= used_today:
                    typer.echo(
                        f"nothing written: the ledger already shows {shown:,} calls for "
                        f"{vendor} on {day}"
                    )
                else:
                    add_manual(sql, vendor, used_today - shown, day=day, now=utcnow)
                    typer.echo(
                        f"wrote {used_today - shown:,} manual calls for {vendor} on {day} "
                        f"(the ledger showed {shown:,})"
                    )
