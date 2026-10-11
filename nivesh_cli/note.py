"""`nivesh ta|fa --note`: the engine result as a saved, checked report (ST-10.3). By default no
model call and no spend; `--analyst` adds one quick analyst call under the budget gate."""

import asyncio
from contextlib import AbstractContextManager
from datetime import date

import typer

from nivesh_adapters.report import save_report, to_json, to_markdown
from nivesh_adapters.report_check import finalize
from nivesh_adapters.research_service import (
    ANALYST_OF,
    analyst_lines,
    analyst_target,
    engine_report,
    note_of,
)
from nivesh_agents.analysts import run_analyst
from nivesh_agents.context import RunCtx
from nivesh_agents.prompts import load_prompt
from nivesh_cli.common import Metered, metered_run, plain_run, profile_of, settings_of, user_errors
from nivesh_cli.engine import _as_of, reader
from nivesh_core.security_master import SecurityRow
from nivesh_core.timeutil import utcnow


def _ask(
    ctx: typer.Context, m: Metered, kind: str, row: SecurityRow, day: date
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One quick analyst call; a failure comes back as a gap, never an error."""
    settings = settings_of(ctx)
    cfg, name = settings.agents, ANALYST_OF[kind]
    m.tracer.start(kind, f"{name}:v{load_prompt(name, pins=cfg.prompt_pins).version}")
    run = RunCtx(cfg, m.tracer, day, "quick", settings.prices, settings.usd_inr, m.env)
    got = asyncio.run(run_analyst(run, name, analyst_target(row)))
    m.tracer.finish_run(getattr(cfg.models, cfg.tiers[name]))
    return analyst_lines(got)


def run_note(
    ctx: typer.Context, kind: str, security: str, as_of: str | None, as_json: bool,
    analyst: bool = False,
) -> None:  # fmt: skip
    settings = settings_of(ctx)
    with user_errors():
        with reader(ctx) as r:
            day = _as_of(as_of)
            rep, row = engine_report(r.duck, r.sql, r.settings, kind, security, day)
        run: AbstractContextManager[Metered] = (
            metered_run(settings, profile_of(ctx), kind, "quick", force=False)
            if analyst
            else plain_run(settings, kind)
        )
        with run as m:
            lines, gaps = _ask(ctx, m, kind, row, day) if analyst else ((), ())
            note, facts = note_of(rep, row, kind, day, utcnow(), m.run_id, lines, gaps)
            m.status = "ok"
            note = finalize(
                note, facts, m.run_dir, max_digits=settings.report.max_tolerance_digits,
                metered=m,
            )  # fmt: skip
            save_report(m.run_dir, note, facts, m.tracer)
    if note.banner:
        typer.echo(f"warning: {note.banner}; see {m.run_dir}/report.md", err=True)
    typer.echo(to_json(note) if as_json else to_markdown(note))
    typer.echo(f"saved: {m.run_dir}", err=True)
