"""`nivesh brief [india|us|both]`: a one-page market brief from the stored data (ST-10.4).

Deterministic: no model call and no spend. At most 400 words of text plus one table; whatever
the stores cannot supply is listed under data gaps.
"""

from dataclasses import replace
from typing import Annotated

import typer

from nivesh_adapters.brief_service import CHOICES, brief_input
from nivesh_adapters.report import save_report, to_json, to_markdown
from nivesh_adapters.report_check import finalize
from nivesh_adapters.report_templates import brief_report
from nivesh_cli.common import plain_run, profile_of, settings_of, user_errors
from nivesh_cli.deliver import DeliverFlag, after_save
from nivesh_cli.engine import AsJson, _as_of, reader
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
        with reader(ctx) as rd:
            day = _as_of(None)
            data = brief_input(
                rd.duck, rd.sql, settings, profile_of(ctx), market.lower(), day, utcnow()
            )  # fmt: skip
        with plain_run(settings, "brief") as m:
            m.status = "ok"
            report = finalize(
                brief_report(replace(data, run_id=m.run_id)), {"as_of": day}, m.run_dir,
                max_digits=settings.report.max_tolerance_digits, metered=m,
            )  # fmt: skip
            save_report(m.run_dir, report, {"as_of": day}, m.tracer)
            after_save(settings, m, deliver)
    if report.banner:
        typer.echo(f"warning: {report.banner}; see {m.run_dir}/report.md", err=True)
    typer.echo(to_json(report) if as_json else to_markdown(report))
    typer.echo(f"saved: {m.run_dir}", err=True)
