import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from nivesh_agents.runtime import run_command, safe_error
from nivesh_core.config import Settings, load_settings
from nivesh_core.db import init_stores
from nivesh_core.errors import NiveshError, SecretNotFound
from nivesh_core.profile import Profile, load_profile
from nivesh_core.secrets import SecretRef

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Nivesh research agent.")
mcp_app = typer.Typer(no_args_is_help=True, help="MCP server tools.")
app.add_typer(mcp_app, name="mcp")


@contextmanager
def user_errors() -> Iterator[None]:
    """Expected failures become a clean message on stderr and a non-zero exit."""
    try:
        yield
    except NiveshError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


@app.callback()
def _startup(
    ctx: typer.Context,
    config_dir: Annotated[Path, typer.Option(envvar="NIVESH_CONFIG_DIR")] = Path("config"),
) -> None:
    """Validate config/nivesh.yaml and config/profile.yaml; fail fast on bad values."""
    with user_errors():
        ctx.obj = (
            load_settings(config_dir / "nivesh.yaml"),
            load_profile(config_dir / "profile.yaml"),
        )


def _settings(ctx: typer.Context) -> Settings:
    settings: Settings = ctx.obj[0]
    return settings


def _profile(ctx: typer.Context) -> Profile:
    profile: Profile = ctx.obj[1]
    return profile


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
) -> None:
    """Run a command headless via the Agent SDK; exits non-zero on any failure."""
    settings = _settings(ctx)
    env: dict[str, str] = {}
    if settings.anthropic_api_key:
        try:
            env["ANTHROPIC_API_KEY"] = SecretRef(settings.anthropic_api_key).resolve()
        except SecretNotFound:
            pass  # fall back to an existing Claude Code login / environment
    try:
        out = asyncio.run(
            run_command(command, args or [], mode=settings.mode, refresh=refresh, env=env)
        )
    except Exception as e:  # noqa: BLE001 - every failure must become a clean non-zero exit
        typer.echo(f"error: {safe_error(e)}", err=True)
        raise typer.Exit(1) from None
    typer.echo(out)
