"""`nivesh deliver RUN` and the `--deliver` flag: send a saved report to the owner's own
channels (ST-10.7). Opt-in through `delivery.channels`; a failed send warns and never changes
the exit code or the run it follows."""

import logging
from pathlib import Path
from typing import Annotated

import httpx
import typer

from nivesh_adapters.delivery import (
    Outcome,
    SmtpFactory,
    build_transports,
    deliver_run,
    holdings_run,
    ssl_smtp,
)
from nivesh_cli.common import Metered, plain_run, settings_of, user_errors
from nivesh_core.config import Settings
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.delivery_config import CHANNELS, Channel
from nivesh_core.errors import NiveshError
from nivesh_core.paths import find_run_dir
from nivesh_core.trace import Tracer

deliver_app = typer.Typer()
DeliverFlag = Annotated[
    bool, typer.Option("--deliver", help="Also send the saved report to delivery.channels.")
]


def http_client(timeout: int) -> httpx.Client:
    """Plain client, never the replay recorder: record mode would persist the address. The
    http libraries log every request URL at INFO, and the address carries the bot or hook."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return httpx.Client(timeout=timeout)


def smtp_factory() -> SmtpFactory:
    return ssl_smtp


def _where(settings: Settings, run_dir: Path) -> str:
    try:
        return run_dir.relative_to(Path(settings.data_dir)).as_posix()
    except ValueError:
        return run_dir.name


def send_saved(
    settings: Settings, run_dir: Path, tracer: Tracer | None, only: Channel | None = None
) -> list[Outcome]:
    """Send the report saved in `run_dir`; one line per channel on stderr, never a URL."""
    cfg = settings.delivery
    if not cfg.channels:
        typer.echo("deliver: no delivery channels configured; nothing sent", err=True)
        return []
    with http_client(cfg.timeout_s) as client:
        transports = build_transports(cfg, client, smtp_factory())
        out = deliver_run(
            run_dir, cfg, transports, tracer, where=_where(settings, run_dir), only=only
        )
    for o in out:
        tail = f" ({o.detail})" if o.detail else ""
        typer.echo(f"deliver: {o.channel} {o.status}{tail}", err=True)
        if o.status in ("blocked", "failed", "unconfigured"):
            typer.echo(f"warning: {o.channel} not delivered; the report is saved", err=True)
    return out


def after_save(settings: Settings, m: Metered, on: bool) -> None:
    """The `--deliver` hook: call right after `save_report`, inside the run."""
    if on:
        send_saved(settings, m.run_dir, m.tracer)


def _command_of(settings: Settings, run_id: int) -> str:
    conn = open_sqlite(Path(settings.data_dir) / "nivesh.sqlite")
    try:
        row = conn.execute("SELECT command FROM run WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


@deliver_app.command("deliver")
def deliver_cmd(
    ctx: typer.Context,
    run_id: Annotated[int, typer.Argument(help="Run number of a saved report.")],
    channel: Annotated[
        str | None, typer.Option("--channel", help="Only this channel: telegram, slack, email.")
    ] = None,
    include_holdings: Annotated[
        bool, typer.Option("--include-holdings", help="Also send reports that list holdings.")
    ] = False,
) -> None:
    """Send the report saved by a run to the configured channels (fails closed on personal data)."""
    if channel is not None and channel not in CHANNELS:
        raise typer.BadParameter(f"must be one of {', '.join(CHANNELS)}", param_hint="--channel")
    settings = settings_of(ctx)
    only: Channel | None = channel if channel in CHANNELS else None
    with user_errors():
        found = find_run_dir(Path(settings.data_dir), run_id)
        if found is None:
            raise NiveshError(f"no run {run_id}")
        if holdings_run(_command_of(settings, run_id)) and not include_holdings:
            raise NiveshError(
                f"run {run_id} lists holdings; not sent. Pass --include-holdings to send it"
            )
        with plain_run(settings, "deliver") as m:
            send_saved(settings, found, m.tracer, only)
            m.status = "ok"
