"""`nivesh research TICKER [quick|deep]`: the full committee on one security, saved as a
checked research note (ST-10.3). Spends model budget under the same gate as `ideas`."""

import asyncio
from typing import Annotated

import typer

from nivesh_adapters.report import save_report, to_json, to_markdown
from nivesh_adapters.report_check import finalize
from nivesh_adapters.research_service import ResearchFacts, gather_research, research_report
from nivesh_agents.committee import CommitteeInputs, prepare_inputs, run_committee
from nivesh_agents.prompts import load_prompt
from nivesh_cli.common import metered_run, profile_of, settings_of, user_errors
from nivesh_cli.deliver import DeliverFlag, after_save
from nivesh_cli.engine import AsJson, Symbol, _as_of, reader
from nivesh_core.errors import NiveshError
from nivesh_core.timeutil import utcnow

research_app = typer.Typer()
TIERS = ("quick", "deep")


def _prepare(ctx: typer.Context, ticker: str) -> tuple[CommitteeInputs, ResearchFacts]:
    settings, profile = settings_of(ctx), profile_of(ctx)
    with reader(ctx) as rd:  # closed again before any agent call
        day = _as_of(None)
        inputs = prepare_inputs(
            rd.duck, rd.sql, settings, profile, [ticker], day,
            starter_weight_pct=settings.agents.starter_weight_pct,
        )  # fmt: skip
        sid = inputs.securities[0].target.security_id
        return inputs, gather_research(rd.duck, rd.sql, settings, sid, day)


@research_app.command("research")
def research_cmd(
    ctx: typer.Context,
    ticker: Symbol,
    tier: Annotated[str, typer.Argument(help="quick or deep (default: deep).")] = "deep",
    force: Annotated[bool, typer.Option("--force", help="Allow a deep run near the cap.")] = False,
    as_json: AsJson = False,
    deliver: DeliverFlag = False,
) -> None:
    """Run the committee on one security and save the research note beside its trace."""
    if tier not in TIERS:
        raise typer.BadParameter(f"must be one of {', '.join(TIERS)}", param_hint="TIER")
    settings, profile = settings_of(ctx), profile_of(ctx)
    cfg = settings.agents
    with user_errors():
        inputs, facts = _prepare(ctx, ticker)
        with metered_run(settings, profile, "research", tier, force=force) as m:
            m.tracer.start("research", f"pm:v{load_prompt('pm', pins=cfg.prompt_pins).version}")
            result = asyncio.run(
                run_committee(
                    inputs,
                    tier=m.tier,
                    cfg=cfg,
                    settings=settings,
                    tracer=m.tracer,
                    run_dir=m.run_dir,
                    run_id=m.run_id,
                    prices=settings.prices,
                    usd_inr=settings.usd_inr,
                    env=m.env,
                )  # fmt: skip
            )
            if not result.results:
                raise NiveshError(f"the committee returned nothing for {ticker!r}")
            m.status = "ok"
            try:  # the check may lower the status to needs_review, never raise it
                note, shown = research_report(
                    result.results[0], facts, inputs.securities[0].card, utcnow(), m.run_id
                )
                note = finalize(
                    note, shown, m.run_dir, max_digits=settings.report.max_tolerance_digits,
                    metered=m,
                )  # fmt: skip
                save_report(m.run_dir, note, shown, m.tracer)
                after_save(settings, m, deliver)
            except Exception:
                m.status = "error"
                raise
    if note.banner:
        typer.echo(f"warning: {note.banner}; see {m.run_dir}/report.md", err=True)
    typer.echo(to_json(note) if as_json else to_markdown(note))
    typer.echo(f"saved: {m.run_dir}", err=True)
