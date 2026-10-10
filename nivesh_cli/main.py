import asyncio
from pathlib import Path
from typing import Annotated

import typer

from nivesh_agents.runtime import run_command, safe_error
from nivesh_cli.common import metered_run, profile_of, settings_of, user_errors
from nivesh_cli.engine import engine_app
from nivesh_cli.holdings import holdings_app
from nivesh_cli.ideas import ideas_app
from nivesh_cli.market import market_app, master_app
from nivesh_cli.mf import mf_app
from nivesh_cli.review import review_app
from nivesh_cli.thesis import thesis_app
from nivesh_cli.universe import universe_app
from nivesh_cli.watch import watch_app
from nivesh_core.config import Settings, load_settings
from nivesh_core.cost import gate, month_to_date
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.egress import check_egress
from nivesh_core.errors import ConfigError
from nivesh_core.profile import Profile, load_profile
from nivesh_core.secrets import secret_exists, set_secret
from nivesh_core.timeutil import utcnow

TIERS = ("brief", "quick", "deep")
app = typer.Typer(no_args_is_help=True, add_completion=False, help="Nivesh research agent.")
mcp_app = typer.Typer(no_args_is_help=True, help="MCP server tools.")
app.add_typer(mcp_app, name="mcp")
secrets_app = typer.Typer(no_args_is_help=True, help="Manage secrets in the OS keychain.")
app.add_typer(secrets_app, name="secrets")
app.registered_commands.extend(holdings_app.registered_commands)  # login, sync, ingest, ...
app.registered_commands.extend(engine_app.registered_commands)  # ta, fa, valuation, ...
app.registered_commands.extend(ideas_app.registered_commands)  # ideas
app.add_typer(master_app, name="master")
app.add_typer(market_app, name="market")
app.add_typer(mf_app, name="mf")
app.add_typer(thesis_app, name="thesis")
app.add_typer(review_app, name="review")
app.add_typer(universe_app, name="universe")
app.add_typer(watch_app, name="watch")


@app.callback()
def _startup(
    ctx: typer.Context,
    config_dir: Annotated[Path, typer.Option(envvar="NIVESH_CONFIG_DIR")] = Path("config"),
) -> None:
    """Validate config/nivesh.yaml and config/profile.yaml; fail fast on bad values."""
    ctx.meta["config_dir"] = config_dir
    with user_errors():
        ctx.obj = (
            load_settings(config_dir / "nivesh.yaml"),
            load_profile(config_dir / "profile.yaml"),
        )


def _settings(ctx: typer.Context) -> Settings:
    return settings_of(ctx)


def _profile(ctx: typer.Context) -> Profile:
    return profile_of(ctx)


@app.command()
def init(
    ctx: typer.Context,
    data_dir: Annotated[Path | None, typer.Option(help="Defaults to config data_dir.")] = None,
) -> None:
    """Create the SQLite and DuckDB stores (owner-only) at the latest schema version."""
    target = data_dir or Path(_settings(ctx).data_dir)
    with user_errors():
        init_stores(target)
    typer.echo(f"initialised {target}")


@secrets_app.command("set")
def secrets_set(name: Annotated[str, typer.Argument(help="UPPER_SNAKE secret name.")]) -> None:
    """Store a secret in the OS keychain (hidden prompt; the value is never an argument)."""
    with user_errors():
        set_secret(name, typer.prompt("Value", hide_input=True))
    typer.echo(f"stored {name}")


@secrets_app.command("check")
def secrets_check(name: str) -> None:
    """Report whether a secret is set (never prints the value)."""
    typer.echo(f"{name}: {'set' if secret_exists(name) else 'missing'}")


@app.command("pii-scan")
def pii_scan(paths: Annotated[list[Path], typer.Argument()]) -> None:
    """Scan files for PII; prints path:line:kind (never the match); exit 1 if any found."""
    from nivesh_core.pii_scan import scan_paths

    found = scan_paths(paths)
    for f, line, kind in found:
        typer.echo(f"{f}:{line}:{kind}")
    raise typer.Exit(1 if found else 0)


@mcp_app.command("list")
def mcp_list() -> None:
    """Print every registered MCP server and its tools."""
    from nivesh_mcp.registry import SERVERS

    for name, server in SERVERS.items():
        for tool in server.tool_names:
            typer.echo(f"{name}: {tool}")


@app.command()
def run(
    ctx: typer.Context,
    command: Annotated[str, typer.Argument(help="Name of a .claude/commands/ prompt.")],
    args: Annotated[list[str] | None, typer.Argument()] = None,
    refresh: Annotated[bool, typer.Option(help="Bypass the cache for this run.")] = False,
    tier: Annotated[str, typer.Option(help="brief | quick | deep (budget gate input).")] = "quick",
    force: Annotated[bool, typer.Option(help="Skip the 80% deep->quick downgrade.")] = False,
) -> None:
    """Run a command headless via the Agent SDK; records a run row + trace; exit 1 on failure."""
    settings = _settings(ctx)
    if tier not in TIERS:
        typer.echo(f"error: --tier must be one of {', '.join(TIERS)}", err=True)
        raise typer.Exit(2)
    out = ""
    with metered_run(settings, _profile(ctx), command, tier, force=force) as m:
        try:
            out = asyncio.run(
                run_command(
                    command, args or [], mode=settings.mode, refresh=refresh, env=m.env,
                    tracer=m.tracer, prices=settings.prices, usd_inr=settings.usd_inr,
                )
            )  # fmt: skip
            m.status = "ok"
        except Exception as e:  # noqa: BLE001 - every failure must become a clean non-zero exit
            typer.echo(f"error: {safe_error(e)}", err=True)
    if m.status != "ok":
        raise typer.Exit(1)
    typer.echo(out)


@app.command()
def replay(ctx: typer.Context, run_id: int) -> None:
    """Re-run a recorded run's engine steps and require identical results."""
    from nivesh_core.replay import replay_run

    rdir = Path(_settings(ctx).data_dir) / "runs" / str(run_id)
    if not (rdir / "trace.jsonl").is_file():
        typer.echo(f"error: no trace for run {run_id}", err=True)
        raise typer.Exit(1)
    result = replay_run(rdir)
    if not result.steps:
        typer.echo("no engine steps to replay")
    elif result.ok:
        typer.echo(f"replayed {result.steps} steps, identical")
    else:
        for f in result.failures:
            typer.echo(f, err=True)
        raise typer.Exit(1)


@app.command("egress-check")
def egress_check(ctx: typer.Context) -> None:
    """Compare the public egress IP with `registered_ip` (exit 1 on mismatch)."""
    res = check_egress(_settings(ctx).registered_ip, _settings(ctx).egress_url)
    typer.echo(res.message)
    raise typer.Exit(1 if res.status == "mismatch" else 0)


@app.command("backup")
def backup_cmd(ctx: typer.Context) -> None:
    """Write an encrypted backup to `backup.target`, then prune archives past retention."""
    from nivesh_core import backup

    cfg = _settings(ctx).backup
    with user_errors():
        if not cfg.target or not cfg.recipient:
            raise ConfigError("set backup.target and backup.recipient in config/nivesh.yaml")
        target, now = Path(cfg.target), utcnow()
        dest = backup.create_backup(Path(_settings(ctx).data_dir), target, cfg.recipient, now)
        backup.prune(target, cfg.retention_days, now)
    typer.echo(f"wrote {dest}")


@app.command()
def restore(
    ctx: typer.Context,
    archive: Path,
    identity: Annotated[Path, typer.Option(help="age identity (private key) file.")],
    data_dir: Annotated[Path | None, typer.Option(help="Defaults to config data_dir.")] = None,
) -> None:
    """Restore an encrypted backup into a fresh data dir."""
    from nivesh_core import backup

    target = data_dir or Path(_settings(ctx).data_dir)
    with user_errors():
        backup.restore_backup(archive, target, identity)
    typer.echo(f"restored into {target}")


@app.command()
def status(ctx: typer.Context) -> None:
    """Month-to-date cost against the cap and the state of the budget gate."""
    data_dir = Path(_settings(ctx).data_dir)
    with user_errors():
        init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        spent = month_to_date(conn, utcnow())
    finally:
        conn.close()
    cap = _profile(ctx).monthly_cost_cap
    pct = f"{spent / cap * 100:.0f}%" if cap else "n/a"
    state = "disabled" if not cap else gate(spent, cap, "deep", False).message or "ok"
    typer.echo(f"month-to-date: {spent:.2f} INR\ncap: {cap:.2f} INR ({pct})\ngate: {state}")
