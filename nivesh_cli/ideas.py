"""`nivesh ideas [india|us|both] [n] [preset]` (ST-9.4): a few well-argued ideas instead of a
ticker list. A preset screens the named universe, the shortlist is scored once, the research
committee (deep tier) runs on the shortlist only, and the top n upward verdicts are reported
with thesis, entry zone, invalidation, weight and what would prove them wrong.

Every fact is read first with the stores opened for reading only and closed before any agent
runs (the engine server opens them). Every committee verdict is then recorded in the ledger,
the top n flagged as reported. Output is an example for the owner to judge, not advice.
"""

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from nivesh_adapters.analysis_service import plain
from nivesh_adapters.ideas_service import (
    IdeaFacts,
    MarketIdeas,
    gather_facts,
    ledger_rows,
    load_preset,
    render_idea,
    shortlist_for,
)
from nivesh_adapters.universe_service import MARKETS
from nivesh_agents.committee import CommitteeInputs, CommitteeResult, prepare_inputs, run_committee
from nivesh_agents.prompts import load_prompt
from nivesh_cli.common import config_dir_of, metered_run, profile_of, settings_of, user_errors
from nivesh_cli.engine import AsJson, _as_of, reader
from nivesh_core.config import Settings
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.ledger import record_call
from nivesh_core.profile import Profile
from nivesh_engine.shortlist import IdeaVerdict, Ranking, ShortRow, rank_ideas

ideas_app = typer.Typer()
ALL = {"both": ["IN", "US"], **{k: [v] for k, v in MARKETS.items()}}


def _bad(message: str, hint: str) -> typer.BadParameter:
    return typer.BadParameter(message, param_hint=hint)


@ideas_app.command("ideas")
def ideas_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Argument(help="india, us or both.")] = "both",
    n: Annotated[
        int | None, typer.Argument(help="Ideas to report (default: ideas.default_n).")
    ] = None,
    preset: Annotated[str | None, typer.Argument(help="Preset name from config/screens.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Allow a deep run near the cap.")] = False,
    as_json: AsJson = False,
) -> None:
    """Screen, shortlist, run the committee on the shortlist, and report the top n ideas."""
    settings, profile = settings_of(ctx), profile_of(ctx)
    cfg = settings.ideas
    markets = ALL.get(market.lower())
    if markets is None:
        raise _bad(f"must be one of {', '.join(ALL)}", "MARKET")
    count = cfg.default_n if n is None else n
    if count < 1:
        raise _bad("must be at least 1", "N")
    name = preset or cfg.default_preset
    try:
        rules = load_preset(config_dir_of(ctx), name, max_rules=settings.analysis.screen.max_rules)
    except NiveshError as e:
        raise _bad(str(e), "PRESET") from None
    with user_errors():
        with reader(ctx) as rd:
            day = _as_of(None)
            found = [
                shortlist_for(rd.duck, rd.sql, settings, profile, rules, m, day) for m in markets
            ]
            picks = [(mi, row) for mi in found for row in mi.short.rows]
            _check_cap(found, len(picks), settings)
            facts = gather_facts(rd.duck, rd.sql, settings, [r.security_id for _, r in picks], day)
            cards = {k: v for mi in found for k, v in mi.cards.items()}
            inputs = None
            if picks:
                inputs = prepare_inputs(
                    rd.duck, rd.sql, settings, profile, [f"id:{r.security_id}" for _, r in picks],
                    day, starter_weight_pct=settings.agents.starter_weight_pct, cards=cards,
                )  # fmt: skip
        notes = [w for mi in found for w in mi.warnings] + [
            x for mi in found for x in mi.short.notes
        ]
        if inputs is None:
            _report_none(found, name, day, notes, as_json)
            return
        typer.echo(f"ideas: {len(picks)} deep committee runs (estimate)", err=True)
        result, run_id, results = _run(settings, profile, inputs, force)
        ranking = rank_ideas(
            [_idea(v, facts, cards) for v in result.verdicts], count
        )  # ranked by conviction, then composite
        rows = ledger_rows(
            result.verdicts, run_id=run_id, run_dir=results, facts=facts,
            preset_of={r.security_id: name for _, r in picks}, reported_ids=ranking.reported_ids,
        )  # fmt: skip
        sql = open_sqlite(Path(settings.data_dir) / "nivesh.sqlite")
        try:
            for row in rows:
                record_call(sql, row)
        finally:
            sql.close()
    _report(found, picks, result, ranking, facts, name, day, notes, run_id, len(rows), as_json)


def _check_cap(found: list[MarketIdeas], total: int, settings: Settings) -> None:
    cfg = settings.ideas
    for mi in found:
        if len(mi.short.rows) > cfg.shortlist_max:  # config validation makes this unreachable
            raise NiveshError(f"shortlist of {len(mi.short.rows)} exceeds ideas.shortlist_max")
    if total > cfg.max_runs:
        raise NiveshError(
            f"this would run {total} committee runs, above ideas.max_runs ({cfg.max_runs}); "
            "ask for one market or lower ideas.shortlist_size"
        )


def _idea(v: Any, facts: IdeaFacts, cards: dict[int, Any]) -> IdeaVerdict:
    card = cards.get(v.security_id)
    return IdeaVerdict(
        v.security_id, facts.names[v.security_id].symbol, v.verdict, v.conviction,
        None if card is None else card.composite, v.vetoed_by_risk,
    )  # fmt: skip


def _run(
    settings: Settings, profile: Profile, inputs: CommitteeInputs, force: bool,
) -> tuple[CommitteeResult, int, Path]:  # fmt: skip
    cfg = settings.agents
    with metered_run(settings, profile, "ideas", "deep", force=force) as m:
        m.tracer.start("ideas", f"pm:v{load_prompt('pm', pins=cfg.prompt_pins).version}")
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
        m.status = "ok"
        return result, m.run_id, m.run_dir


def _report_none(
    found: list[MarketIdeas], name: str, day: date, notes: list[str], as_json: bool
) -> None:
    reasons = [mi.reason for mi in found if mi.reason]
    if as_json:
        out = {"command": "ideas", "as_of": day.isoformat(), "preset": name, "ideas": [],
               "message": "no candidate matched; nothing was run", "reasons": reasons,
               "warnings": notes}  # fmt: skip
        typer.echo(json.dumps(out, sort_keys=True))
        return
    typer.echo(f"ideas as of {day} (preset {name})")
    for line in (*notes,):
        typer.echo(f"warning: {line}")
    for r in reasons:
        typer.echo(r)
    typer.echo("no candidate matched; nothing was run and nothing was spent")


def _report(
    found: list[MarketIdeas], picks: list[tuple[MarketIdeas, ShortRow]], result: CommitteeResult,
    ranking: Ranking, facts: IdeaFacts, name: str, day: date, notes: list[str], run_id: int,
    recorded: int, as_json: bool,
) -> None:  # fmt: skip
    by_id = {v.security_id: v for v in result.verdicts}
    label_of = {r.security_id: r.label for _, r in picks}
    skipped = [
        {"symbol": facts.names[v.security_id].symbol, "reason": "no verdict (insufficient data)"}
        for v in result.verdicts
        if v.verdict == "INSUFFICIENT_DATA"
    ]
    if as_json:
        ideas = [
            {
                "rank": i, "symbol": x.symbol, "market": facts.names[x.security_id].market,
                "label": label_of.get(x.security_id, ""),
                "verdict": by_id[x.security_id].model_dump(mode="json"),
            }
            for i, x in enumerate(ranking.ideas, start=1)
        ]  # fmt: skip
        out = {
            "command": "ideas", "as_of": day.isoformat(), "preset": name, "run_id": run_id,
            "ideas": ideas, "message": ranking.message, "qualified": ranking.qualified,
            "skipped": skipped, "warnings": notes, "ledger_rows": recorded,
            "shortlists": {mi.market: [r.symbol for r in mi.short.rows] for mi in found},
        }  # fmt: skip
        typer.echo(json.dumps(plain(out), sort_keys=True))
        return
    typer.echo(f"ideas as of {day} (deep committee, preset {name}; examples to judge, not advice)")
    for w in notes:
        typer.echo(f"warning: {w}")
    for mi in found:
        typer.echo(f"{mi.market}: {mi.matches} matches, {len(mi.short.rows)} shortlisted")
    for i, x in enumerate(ranking.ideas, start=1):
        for line in render_idea(
            i, by_id[x.security_id], facts.names[x.security_id], label_of.get(x.security_id, ""),
            name,
        ):  # fmt: skip
            typer.echo(line)
    if ranking.message:
        typer.echo(ranking.message)
    for s in skipped:
        typer.echo(f"skipped {s['symbol']}: {s['reason']}")
    typer.echo(f"recorded {recorded} calls in the ledger (run {run_id})")
