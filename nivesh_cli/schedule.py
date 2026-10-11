"""`nivesh schedule run | status | invalidation`: the headless scheduler tick (ST-11.1).

`run` is meant to be called every five minutes by cron, a systemd timer or launchd (examples are in
the deploy docs; nothing is installed by this program). It is idempotent: a job runs once per
trading date. Times are shown in IST; stored times are UTC.
"""

import json
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from nivesh_adapters.analysis_service import resolve_security
from nivesh_adapters.delivery import Message, Outcome, build_transports, send_message
from nivesh_adapters.schedule_jobs import default_registry
from nivesh_adapters.scheduler import Notifier, calendars_for, run_due
from nivesh_cli.common import config_dir_of, profile_of, settings_of, user_errors
from nivesh_cli.deliver import http_client, smtp_factory
from nivesh_cli.thesis import store_reader
from nivesh_core.alerts import Alert, JobRow, alerts_since, job_rows, set_invalidation
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_text
from nivesh_core.schedule_config import JobSpec, load_schedule
from nivesh_core.security_master import SecurityMaster
from nivesh_core.timeutil import IST, ist_date, to_iso, utcnow
from nivesh_engine.schedule import due_jobs, next_instant

schedule_app = typer.Typer(no_args_is_help=True, help="Run and inspect the scheduled jobs.")
alerts_app = typer.Typer()
ALERT_DAYS = 7
Now = Annotated[
    str | None, typer.Option("--now", help="ISO instant with an offset (default: the clock).")
]
AsJson = Annotated[bool, typer.Option("--json", help="Print JSON.")]


def _now(value: str | None) -> datetime:
    if value is None:
        return utcnow()
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("must be an ISO timestamp", param_hint="--now") from None
    if at.tzinfo is None:
        raise typer.BadParameter("must carry a UTC offset, e.g. +05:30", param_hint="--now")
    return at


def real_notifier(settings: Settings) -> Notifier:
    """Send through the owner's configured channels; nothing when none is configured."""

    def notify(msg: Message) -> list[Outcome]:
        cfg = settings.delivery
        if not cfg.channels:
            return []
        with http_client(cfg.timeout_s) as client:
            transports = build_transports(cfg, client, smtp_factory())
            return send_message(msg, transports, cfg, None, sleep=time.sleep)

    return notify


def _ist(at: datetime | None) -> str:
    return "-" if at is None else at.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


@schedule_app.command("run")
def run_cmd(
    ctx: typer.Context,
    now: Now = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what is due; write and send nothing.")
    ] = False,
) -> None:
    """Run every job that is due, once. Safe to call every five minutes."""
    at = _now(now)
    settings = settings_of(ctx)
    with user_errors():
        schedule = load_schedule(config_dir_of(ctx))
        reports = run_due(
            settings, profile_of(ctx), schedule, default_registry(real_notifier(settings)), at,
            real_notifier(settings), time.sleep, dry_run=dry_run,
        )  # fmt: skip
    if not reports:
        pending = due_jobs(schedule.jobs, calendars_for(settings), at, set(), schedule.us_lead_min)
        typer.echo("already done" if pending else "nothing due")
        return
    for r in reports:
        late = " LATE" if r.late else ""
        tail = f" ({r.reason})" if r.reason else ""
        typer.echo(f"{r.job} {r.trading_date} {r.status}{late}{tail}")


def _warnings(settings: Settings, floor: int, at: datetime) -> list[str]:
    year = ist_date(at).year
    known = settings.market.nse_holidays.get(year)
    if known is None:
        return [f"NSE holiday list has no dates for {year}; India jobs are skipped until it does"]
    if len(known) < floor:
        return [
            f"NSE holiday list has only {len(known)} dates for {year}; "
            "India jobs may run on an unlisted holiday"
        ]
    return []


def _last(row: JobRow | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "trading_date": row.trading_date.isoformat(), "status": row.status,
        "attempts": row.attempts, "late": row.late, "notified": row.notified,
        "reason": row.reason, "finished_ist": _ist(row.finished_at),
    }  # fmt: skip


def _line(job: JobSpec, row: JobRow | None, nxt: datetime | None) -> str:
    if not job.enabled:
        return f"{job.name}: disabled"
    after = f"next {_ist(nxt) if nxt else 'none soon'}"
    if row is None:
        return f"{job.name}: never run; {after}"
    late = " LATE" if row.late else ""
    why = f" ({row.reason})" if row.reason else ""
    return f"{job.name}: {row.status} {row.trading_date}{late}{why}; {after}"


@schedule_app.command("status")
def status_cmd(ctx: typer.Context, now: Now = None, as_json: AsJson = False) -> None:
    """The last run of every job, what is late or skipped, and when each is next due."""
    at = _now(now)
    settings = settings_of(ctx)
    with user_errors():
        schedule = load_schedule(config_dir_of(ctx))
        with store_reader(ctx) as sql:
            last = {r.job: r for r in job_rows(sql)}
    cals = calendars_for(settings)
    nexts = {
        j.name: next_instant(j, cals, at, schedule.us_lead_min) if j.enabled else None
        for j in schedule.jobs
    }
    notes = _warnings(settings, schedule.min_holidays_warn, at)
    if as_json:
        jobs = [
            {"job": j.name, "enabled": j.enabled, "last": _last(last.get(j.name)),
             "next_ist": _ist(nexts[j.name]) if nexts[j.name] else None}
            for j in schedule.jobs
        ]  # fmt: skip
        typer.echo(json.dumps({"now_ist": _ist(at), "jobs": jobs, "warnings": notes},
                              sort_keys=True))  # fmt: skip
        return
    typer.echo(f"schedule status at {_ist(at)}")
    for j in schedule.jobs:
        typer.echo(_line(j, last.get(j.name), nexts[j.name]))
    for n in notes:
        typer.echo(f"warning: {n}")


@schedule_app.command("invalidation")
def invalidation_cmd(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="ISIN, symbol or name.")],
    level: Annotated[str, typer.Argument(help="Close below this price fires the alert.")],
    note: Annotated[str | None, typer.Option("--note", help="Why this level.")] = None,
) -> None:
    """Record the price below which the thesis of a security is invalidated (owner-supplied)."""
    try:
        value = Decimal(level)
    except InvalidOperation:
        raise typer.BadParameter("must be a number", param_hint="LEVEL") from None
    settings = settings_of(ctx)
    with user_errors():
        if note is not None and scan_text(note):
            raise NiveshError("the note looks like personal data; not saved")
        data_dir = Path(settings.data_dir)
        init_stores(data_dir)
        sql = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            sec = resolve_security(sql, symbol)
            set_invalidation(sql, sec.id, value, ist_date(utcnow()), note)
        finally:
            sql.close()
    typer.echo(f"invalidation level for {sec.symbol} set to {value}")


def _status(a: Alert) -> str:
    if a.delivered == 2:
        return f"sent in digest {a.digest_id}"
    if a.delivered == 1:
        return "sent"
    held = a.payload.get("suppressed")
    if held:
        return f"held back ({held})"
    return "blocked" if a.payload.get("blocked") else "not sent"


@alerts_app.command("alerts")
def alerts_cmd(
    ctx: typer.Context,
    since: Annotated[
        str | None, typer.Option("--since", help="IST date YYYY-MM-DD (default: last 7 days).")
    ] = None,
    rule: Annotated[str | None, typer.Option("--rule", help="Only this rule.")] = None,
    as_json: AsJson = False,
) -> None:
    """The alerts that fired, with what happened, the verdict, and whether they were sent."""
    if since is None:
        start = utcnow() - timedelta(days=ALERT_DAYS)
    else:
        try:
            start = datetime.fromisoformat(date.fromisoformat(since).isoformat()).replace(
                tzinfo=IST
            )
        except ValueError:
            raise typer.BadParameter("must be YYYY-MM-DD", param_hint="--since") from None
    with user_errors(), store_reader(ctx) as sql:
        found = alerts_since(sql, start, rule)
        names = SecurityMaster(sql).get_many({a.security_id for a in found if a.security_id})
    rows = [
        {
            "alert_id": a.alert_id, "rule": a.rule, "fired_at_utc": to_iso(a.fired_at),
            "fired_at_ist": _ist(a.fired_at), "trading_date": a.trading_date.isoformat(),
            "symbol": names[a.security_id].symbol if a.security_id in names else None,
            "status": _status(a), "delivered": a.delivered, "digest_id": a.digest_id,
            "payload": a.payload,
        }
        for a in found
    ]  # fmt: skip
    if as_json:
        typer.echo(json.dumps(rows, sort_keys=True))
        return
    if not rows:
        typer.echo("no alerts")
        return
    for r in rows:
        what = r["payload"].get("what", "")  # type: ignore[union-attr]
        verdict = r["payload"].get("verdict", "-")  # type: ignore[union-attr]
        typer.echo(f"{r['fired_at_ist']}  {r['rule']}  {r['symbol'] or '-'}  {r['status']}")
        typer.echo(f"    {what} | verdict: {verdict}")
