"""`nivesh brief [india|us|both]`: a one-page market brief from the stored data (ST-10.4).

Deterministic: no model call and no spend. At most 400 words of text plus one table; whatever
the stores cannot supply is listed under data gaps.
"""

from typing import Annotated

import typer

from nivesh_adapters.brief_service import CHOICES, run_brief
from nivesh_adapters.report import to_json, to_markdown
from nivesh_cli.common import profile_of, settings_of, user_errors
from nivesh_cli.deliver import DeliverFlag, after_save
from nivesh_cli.engine import AsJson, _as_of
from nivesh_core.timeutil import utcnow

brief_app = typer.Typer()


@brief_app.command("brief")
def brief_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Argument(help="india, us or both.")] = "both",
    as_json: AsJson = False,
    deliver: DeliverFlag = False,
) -> None:
    """Index levels and moves, regime, rates, flows, events and news for what you hold."""
    if market.lower() not in CHOICES:
        raise typer.BadParameter(f"must be one of {', '.join(CHOICES)}", param_hint="MARKET")
    settings = settings_of(ctx)
    with user_errors():
        out = run_brief(
            settings, profile_of(ctx), market.lower(), _as_of(None), clock=lambda: utcnow(),
            deliver_fn=lambda m: after_save(settings, m, deliver),
        )  # fmt: skip
    if out.report.banner:
        typer.echo(f"warning: {out.report.banner}; see {out.run_dir}/report.md", err=True)
    typer.echo(to_json(out.report) if as_json else to_markdown(out.report))
    typer.echo(f"saved: {out.run_dir}", err=True)
