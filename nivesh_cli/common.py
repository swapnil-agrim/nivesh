from collections.abc import Iterator
from contextlib import contextmanager

import typer

from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError


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
