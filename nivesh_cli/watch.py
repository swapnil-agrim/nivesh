"""`nivesh watch add TICKER [LOW HIGH]` and `nivesh watch` (ST-9.5): track a security with an
optional entry zone, and see how far its latest close is from that zone and what the committee
last said about it. Listing opens the stores read-only. There are no alerts yet.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from nivesh_adapters.analysis_service import plain
from nivesh_adapters.ideas_service import gather_facts
from nivesh_cli.common import settings_of, user_errors
from nivesh_cli.engine import AsJson, _as_of, reader
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.ledger import latest_call
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_core.watch import track_security, watched
from nivesh_engine.shortlist import WatchDistance, watch_distance


@dataclass(frozen=True)
class Said:
    verdict: str
    conviction: str
    review_date: date
    run_id: int


@dataclass(frozen=True)
class WatchRow:
    symbol: str
    market: str
    entry_low: Decimal | None
    entry_high: Decimal | None
    last_close: Decimal | None
    distance: WatchDistance
    verdict: Said | None


watch_app = typer.Typer(help="Securities you track, with an optional entry zone.")


def _number(text: str, what: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise NiveshError(f"{what} must be a number, got {text!r}") from None


def _one(sql: sqlite3.Connection, ticker: str) -> SecurityRow:
    master = SecurityMaster(sql)
    found = master.lookup(ticker)
    row = master.get(found.security_id) if found.security_id is not None else None
    if row is not None:
        return row
    if found.candidates and found.matched_by in ("symbol", "isin"):
        names = ", ".join(f"{c.symbol} ({c.exchange})" for c in found.candidates[:5])
        raise NiveshError(
            f"{ticker!r} is ambiguous across exchanges or markets: {names}; "
            "give the ISIN, or id:<n> from `nivesh universe show`"
        )
    raise NiveshError(f"no security matches {ticker!r}; run `nivesh master build` first")


@watch_app.command("add")
def add_cmd(
    ctx: typer.Context,
    ticker: Annotated[str, typer.Argument(help="Symbol, ISIN or company name.")],
    low: Annotated[str | None, typer.Argument(help="Entry zone, low end.")] = None,
    high: Annotated[str | None, typer.Argument(help="Entry zone, high end.")] = None,
) -> None:
    """Track a security, with an entry zone if you give both ends (it replaces an older zone)."""
    settings = settings_of(ctx)
    with user_errors():
        if (low is None) != (high is None):
            raise NiveshError("give both ends of the entry zone (LOW HIGH), or neither")
        lo = None if low is None else _number(low, "LOW")
        hi = None if high is None else _number(high, "HIGH")
        data_dir = Path(settings.data_dir)
        init_stores(data_dir)
        sql = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            row = _one(sql, ticker)
            track_security(sql, row.id, lo, hi, _as_of(None))
        finally:
            sql.close()
    zone = "no entry zone" if lo is None else f"entry zone {lo} to {hi}"
    typer.echo(f"tracking {row.symbol} ({row.market}), {zone}")


@watch_app.callback(invoke_without_command=True)
def list_cmd(ctx: typer.Context, as_json: AsJson = False) -> None:
    """With no subcommand: distance to the entry zone and the latest committee verdict."""
    if ctx.invoked_subcommand is not None:
        return
    settings = settings_of(ctx)
    out: list[WatchRow] = []
    with user_errors(), reader(ctx) as r:
        day = _as_of(None)
        marks = watched(r.sql)
        facts = gather_facts(r.duck, r.sql, settings, [w.security_id for w in marks], day)
        for w in marks:
            row = facts.names.get(w.security_id)
            last = latest_call(r.sql, w.security_id)
            close = facts.last_close.get(w.security_id)
            out.append(
                WatchRow(
                    row.symbol if row else str(w.security_id),
                    row.market if row else "",
                    w.entry_low,
                    w.entry_high,
                    close,
                    watch_distance(close, w.entry_low, w.entry_high),
                    None
                    if last is None
                    else Said(last.verdict, last.conviction, last.review_date, last.run_id),
                )  # fmt: skip
            )
    if as_json:
        body = {"command": "watch", "as_of": day.isoformat(), "watched": out}
        typer.echo(json.dumps(plain(body), sort_keys=True))
        return
    typer.echo(f"watch as of {day}")
    if not out:
        typer.echo("nothing tracked yet: `nivesh watch add TICKER [LOW HIGH]`")
    for x in out:
        typer.echo(_line(x))


def _line(x: WatchRow) -> str:
    d = x.distance
    if d.percent is None:
        dist = f"distance n/a ({d.reason})"
    elif d.state == "inside":
        dist = "inside the entry zone (0%)"
    else:
        dist = f"{d.percent}% {'above' if d.state == 'above' else 'below'} the entry zone"
    zone = "no zone" if x.entry_low is None else f"zone {x.entry_low} to {x.entry_high}"
    close = "no close" if x.last_close is None else f"close {x.last_close}"
    v = x.verdict
    said = "no verdict yet" if v is None else f"{v.verdict} ({v.conviction}, run {v.run_id})"
    return f"{x.symbol} ({x.market})  {zone}  {close}  {dist}  latest verdict: {said}"
