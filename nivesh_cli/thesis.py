"""`nivesh thesis onboard | list | show` (ST-8.1): draft, review and read investment theses.

Theses cover equity and ETF holdings; mutual funds are reviewed by the fund doctor instead.
`list` and `show` open the store read-only; `onboard` drafts with the agents (a metered run)
and stores only the theses the owner accepts.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from nivesh_adapters.analysis_service import resolve_security
from nivesh_agents.context import RunCtx
from nivesh_agents.prompts import load_prompt
from nivesh_agents.schemas import ThesisDraft
from nivesh_agents.store import RunStore
from nivesh_agents.thesis_agent import (
    THESIS_CLASSES,
    Choice,
    Held,
    apply_choice,
    draft_all,
    merge_held,
    onboard_targets,
)
from nivesh_cli.common import metered_run, profile_of, settings_of, user_errors
from nivesh_cli.engine import AsOf, _as_of
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.thesis import KillCriterion, Thesis
from nivesh_core.thesis_store import active_theses, active_thesis, save_thesis

thesis_app = typer.Typer(no_args_is_help=True, help="Investment theses of equity holdings.")
FUNDS_NOTE = "mutual funds are out of scope here (the fund doctor reviews them)"


def held_securities(sql: sqlite3.Connection) -> list[Held]:
    """Equity and ETF holdings merged per security, largest INR value first."""
    rows: list[Held] = []
    for h in latest_holdings(sql):
        if h.asset_class not in THESIS_CLASSES:
            continue
        row = None
        if h.isin:
            row = sql.execute(
                "SELECT id, name, market FROM security WHERE isin = ? "
                "ORDER BY unresolved, id LIMIT 1",
                (h.isin,),
            ).fetchone()
        if row is None:
            row = sql.execute(
                "SELECT id, name, market FROM security WHERE symbol = ? AND exchange = ?",
                (h.symbol, h.exchange),
            ).fetchone()
        rows.append(Held(int(row[0]), h.symbol, h.asset_class, h.value_inr, row[1] or "", row[2]))
    return merge_held(rows)


@contextmanager
def store_reader(ctx: typer.Context) -> Iterator[sqlite3.Connection]:
    """The SQLite store, opened for reading only."""
    data_dir = Path(settings_of(ctx).data_dir)
    init_stores(data_dir)
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    sql.execute("PRAGMA query_only = ON")
    try:
        yield sql
    finally:
        sql.close()


def _criterion(c: KillCriterion) -> str:
    if c.metric is None:
        return f"{c.criterion_id}. {c.text} [judged by the reviewer]"
    unit = f" {c.unit}" if c.unit else ""
    return f"{c.criterion_id}. {c.text} [{c.metric} {c.comparator} {c.threshold}{unit}]"


@thesis_app.command("list")
def list_cmd(ctx: typer.Context, as_of: AsOf = None) -> None:
    """Active theses with horizon and review date; holdings that still need one."""
    with user_errors(), store_reader(ctx) as sql:
        day = _as_of(as_of)
        theses = active_theses(sql)
        symbols = {r[0]: r[1] for r in sql.execute("SELECT id, symbol FROM security")}
        for sid, t in sorted(theses.items(), key=lambda kv: symbols.get(kv[0], "")):
            due = "  overdue" if t.review_date < day else ""
            typer.echo(f"{symbols.get(sid, sid)}  {t.horizon}  review {t.review_date}{due}")
        missing = [h.symbol for h in held_securities(sql) if h.security_id not in theses]
        typer.echo(f"without a thesis: {', '.join(missing) if missing else 'none'}")
        typer.echo(FUNDS_NOTE)


@thesis_app.command("show")
def show_cmd(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="ISIN, symbol or company name.")],
) -> None:
    """One thesis: why, horizon, review date and each kill criterion."""
    with user_errors(), store_reader(ctx) as sql:
        sec = resolve_security(sql, symbol)
        t = active_thesis(sql, sec.id)
        if t is None:
            raise NiveshError(f"no active thesis for {sec.symbol}; run `nivesh thesis onboard`")
        typer.echo(f"{sec.symbol} ({t.horizon}), created {t.created_at}")
        typer.echo(f"review date: {t.review_date}")
        typer.echo(f"why: {t.why}")
        typer.echo("kill criteria:")
        for c in t.kill_criteria:
            typer.echo(f"  {_criterion(c)}")


def _draft_lines(symbol: str, d: ThesisDraft, gaps: tuple[str, ...]) -> list[str]:
    out = [f"{symbol} ({d.horizon})", f"why: {d.why}", "kill criteria:"]
    out += [f"  {_criterion(c)}" for c in d.kill_criteria]
    gaps_all = [*d.data_gaps, *gaps]
    return out + ([f"data gaps: {'; '.join(gaps_all)}"] if gaps_all else [])


def _ask(label: str, current: object) -> Any:
    shown = "none" if current is None else current
    got = typer.prompt(
        f"  {label} [{shown}] (blank = keep, - = clear)", default="", show_default=False
    ).strip()
    return None if got == "-" else got or current


def _edits(d: ThesisDraft) -> dict[str, Any]:
    """Prompt why, horizon, then each criterion's text, metric, comparator and threshold."""
    out: dict[str, Any] = {"why": _ask("why", d.why), "horizon": _ask("horizon", d.horizon)}
    crit = []
    for c in d.kill_criteria:
        typer.echo(f"  criterion {c.criterion_id}")
        item = {"criterion_id": c.criterion_id, "unit": c.unit}
        for name in ("text", "metric", "comparator", "threshold"):
            value = _ask(name, getattr(c, name))
            item[name] = None if value is None else str(value)
        if item["metric"] is None:
            item["unit"] = ""
        crit.append(item)
    out["kill_criteria"] = crit
    return out


def _decide(draft: ThesisDraft, created: date, review_days: int) -> Thesis | None:
    """Ask accept/edit/skip until the owner skips or gives a valid thesis."""
    names: dict[str, Choice] = {"a": "accept", "e": "edit", "s": "skip"}
    while True:
        choice = names.get(typer.prompt("accept, edit or skip? [a/e/s]").strip().lower()[:1])
        if choice is None:
            continue
        edits = _edits(draft) if choice == "edit" else None
        got = apply_choice(draft, choice, edits, created=created, review_days=review_days)
        if not isinstance(got, list):
            return got
        for e in got:
            typer.echo(f"  invalid: {e}")


@thesis_app.command("onboard")
def onboard_cmd(
    ctx: typer.Context,
    only: Annotated[str | None, typer.Option("--only", help="One holding: ISIN or symbol.")] = None,
) -> None:
    """Draft a thesis for each equity and ETF holding without one; accept, edit or skip each."""
    settings, profile = settings_of(ctx), profile_of(ctx)
    with user_errors(), store_reader(ctx) as sql:
        day = _as_of(None)
        held = held_securities(sql)
        if only is not None:
            sid = resolve_security(sql, only).id
            held = [h for h in held if h.security_id == sid]
        targets = onboard_targets(held, set(active_theses(sql)))
    if not targets:
        typer.echo("every equity and ETF holding has an active thesis; nothing to draft")
        return
    cfg = settings.agents
    with metered_run(settings, profile, "thesis onboard", "quick", force=False) as m:
        version = load_prompt("thesis_draft", pins=cfg.prompt_pins).version
        m.tracer.start("thesis onboard", f"thesis_draft:v{version}")
        run = RunCtx(cfg, m.tracer, day, "quick", settings.prices, settings.usd_inr, m.env)
        outcomes = asyncio.run(draft_all(run, targets, RunStore(m.run_dir, m.tracer)))
        m.tracer.finish_run(getattr(cfg.models, cfg.tiers["thesis_draft"]))
        m.status = "ok"
    data_dir = Path(settings.data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        for o in outcomes:
            if o.draft is None:
                typer.echo(f"{o.target.symbol}: skipped ({o.reason})")
                continue
            for line in _draft_lines(o.target.symbol, o.draft, o.gaps):
                typer.echo(line)
            thesis = _decide(o.draft, day, settings.review.default_review_days)
            if thesis is None:
                typer.echo(f"skipped {o.target.symbol}")
                continue
            with user_errors():
                save_thesis(conn, thesis)
            typer.echo(f"stored thesis for {o.target.symbol}, review {thesis.review_date}")
    finally:
        conn.close()
