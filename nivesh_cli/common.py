from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import typer

from nivesh_adapters.run_support import BudgetRefused, Metered
from nivesh_adapters.run_support import metered_run as _metered_run
from nivesh_adapters.run_support import plain_run as _plain_run
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.profile import Profile
from nivesh_core.timeutil import utcnow

__all__ = ["Metered", "metered_run", "plain_run", "profile_of", "settings_of", "user_errors"]


@contextmanager
def user_errors() -> Iterator[None]:
    """Expected failures become a clean message on stderr and a non-zero exit."""
    try:
        yield
    except NiveshError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


def settings_of(ctx: typer.Context) -> Settings:
    settings: Settings = ctx.obj[0]
    return settings


def config_dir_of(ctx: typer.Context) -> Path:
    found: Path = ctx.meta.get("config_dir", Path("config"))
    return found


def profile_of(ctx: typer.Context) -> Profile:
    profile: Profile = ctx.obj[1]
    return profile


def _warn(message: str) -> None:
    typer.echo(f"warning: {message}", err=True)


@contextmanager
def metered_run(
    settings: Settings, profile: Profile, command: str, tier: str, *, force: bool
) -> Iterator[Metered]:
    """Credentials, egress check, budget gate, run row and tracer around one model-spending
    command. A refused gate exits 1 before any row is written."""
    with ExitStack() as stack:
        try:
            m = stack.enter_context(
                _metered_run(
                    settings,
                    profile,
                    command,
                    tier,
                    force=force,
                    clock=lambda: utcnow(),
                    warn=_warn,
                )  # fmt: skip
            )
        except BudgetRefused as e:
            _warn(str(e))
            raise typer.Exit(1) from None
        except NiveshError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from None
        yield m


@contextmanager
def plain_run(settings: Settings, command: str) -> Iterator[Metered]:
    """A run row, dated directory and trace for a command that spends nothing: no budget gate,
    no egress check, no credentials. Set `status` to "ok" when the work succeeds."""
    with ExitStack() as stack:
        try:
            m = stack.enter_context(_plain_run(settings, command, clock=lambda: utcnow()))
        except NiveshError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from None
        yield m
