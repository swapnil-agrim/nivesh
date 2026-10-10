"""`nivesh ta | fa | valuation | flags | xray | risk | screen | score` (E6 analysis engines).

Thin read-only commands over the stored data and the pure engines in `nivesh_engine`. Nothing is
fetched and nothing is written: both stores are opened for reading only. Every command takes
`--as-of` (default: today in India) and `--json`, which prints exact decimal strings.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import duckdb
import typer

from nivesh_adapters import analysis_service as svc
from nivesh_adapters.analysis_data import load_screen_inputs
from nivesh_adapters.analysis_service import plain
from nivesh_cli.common import settings_of, user_errors
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.profile import Profile
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.metrics import REGISTRY, inputs_needed
from nivesh_engine.screen import parse_rules, screen

engine_app = typer.Typer()

AsOf = Annotated[str | None, typer.Option("--as-of", help="Date, YYYY-MM-DD (default: today).")]
AsJson = Annotated[bool, typer.Option("--json", help="Print exact decimal strings as JSON.")]
Symbol = Annotated[str, typer.Argument(help="ISIN, symbol or company name.")]
NOT_STORED = "no market data store found in {path}; run `nivesh init` and ingest prices first"


def _today() -> date:  # seam: tests pin the date
    return ist_date(utcnow())


def _as_of(value: str | None) -> date:
    if value is None:
        return _today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise NiveshError(f"--as-of must be an ISO date YYYY-MM-DD, got {value!r}") from None


@dataclass
class Reader:
    cfg: AnalysisSettings
    settings: Settings
    profile: Profile
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection


@contextmanager
def reader(ctx: typer.Context) -> Iterator[Reader]:
    """Both stores, opened for reading only."""
    settings = settings_of(ctx)
    data_dir = Path(settings.data_dir)
    init_stores(data_dir)
    try:
        duck = open_duck(data_dir / "nivesh.duckdb", read_only=True)
    except (duckdb.ConnectionException, duckdb.IOException):
        raise NiveshError(
            "the market database is in use by another process; retry shortly"
        ) from None
    sql = open_sqlite(data_dir / "nivesh.sqlite")
    sql.execute("PRAGMA query_only = ON")
    try:
        yield Reader(settings.analysis, settings, ctx.obj[1], sql, duck)
    finally:
        sql.close()
        duck.close()


# ---- output -------------------------------------------------------------------------------------
def _is_metric(v: object) -> bool:
    return isinstance(v, dict) and {"value", "available", "reason"} <= v.keys()


def _scalar(v: object) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _label(item: object, index: int) -> str:
    if isinstance(item, dict):
        for k in ("flag", "symbol", "rule_id", "name", "key", "asset_class", "metric"):
            if isinstance(item.get(k), str):
                return str(item[k])
    return f"[{index}]"


def _render(name: str, v: object, depth: int, out: list[str]) -> None:
    pad = "  " * depth
    if isinstance(v, dict) and _is_metric(v):
        shown = _scalar(v["value"]) if v["available"] else f"unavailable ({v['reason']})"
        out.append(f"{pad}{name}: {shown}")
    elif isinstance(v, dict):
        out.append(f"{pad}{name}:")
        for k, x in v.items():
            _render(k, x, depth + 1, out)
    elif isinstance(v, list) and v and all(isinstance(x, dict | list) for x in v):
        out.append(f"{pad}{name}:")
        for i, x in enumerate(v):
            _render(_label(x, i), x, depth + 1, out)
    elif isinstance(v, list):
        out.append(f"{pad}{name}: " + (", ".join(_scalar(x) for x in v) or "none"))
    else:
        out.append(f"{pad}{name}: {_scalar(v)}")


def emit(command: str, subject: str | None, as_of: date, result: object, as_json: bool) -> None:
    body = plain(result)
    if as_json:
        payload = {
            "command": command, "subject": subject, "as_of": as_of.isoformat(), "result": body,
        }  # fmt: skip
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"{command} {subject + ' ' if subject else ''}as of {as_of.isoformat()}")
    out: list[str] = []
    if isinstance(body, dict):
        for k, x in body.items():
            _render(k, x, 0, out)
    else:
        _render("result", body, 0, out)
    typer.echo("\n".join(out))


# ---- the single-security commands ---------------------------------------------------------------
@engine_app.command("ta")
def ta_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Technical indicators, support and resistance, and the setup, from stored daily bars."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        rep = svc.ta_report(r.duck, r.sql, r.settings, security, day)
    emit("ta", rep.symbol, day, rep.result, as_json)


@engine_app.command("fa")
def fa_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Growth, profitability, balance sheet and cash quality, using filings up to the date."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        rep = svc.fa_report(r.duck, r.sql, r.settings, security, day)
    emit("fa", rep.symbol, day, rep.result, as_json)


@engine_app.command("valuation")
def valuation_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Multiples with history, percentile and peer median, plus the reverse-DCF fair-value range."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        rep = svc.valuation_report(r.duck, r.sql, r.settings, security, day)
    emit("valuation", rep.symbol, day, rep.result, as_json)


@engine_app.command("flags")
def flags_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Red flags with status, severity and evidence; a flag that cannot be tested says so."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        rep = svc.flag_report(r.duck, r.sql, r.settings, security, day)
    emit("flags", rep.symbol, day, rep.result, as_json)


# ---- the portfolio commands ---------------------------------------------------------------------
@engine_app.command("xray")
def xray_cmd(ctx: typer.Context, as_of: AsOf = None, as_json: AsJson = False) -> None:
    """Allocation, drift, concentration, limits, look-through and XIRR of the stored holdings."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        result = svc.xray_report(r.duck, r.sql, r.settings, r.profile, day)
    emit("xray", None, day, result, as_json)


@engine_app.command("risk")
def risk_cmd(
    ctx: typer.Context,
    candidate: Annotated[str | None, typer.Option(help="A security to test adding.")] = None,
    weight: Annotated[
        str | None, typer.Option(help="Proposed portfolio weight of the candidate, percent.")
    ] = None,
    as_of: AsOf = None,
    as_json: AsJson = False,
) -> None:
    """Volatility, drawdown, beta, days to trade and the pro-forma weights with a candidate."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        result = svc.risk_report(r.duck, r.sql, r.settings, r.profile, day, candidate, weight)
    emit("risk", candidate, day, result, as_json)


# ---- the universe commands ----------------------------------------------------------------------
@engine_app.command("screen")
def screen_cmd(
    ctx: typer.Context,
    rules: Annotated[Path, typer.Option(help="YAML rule file.")],
    security: Annotated[
        list[str] | None, typer.Option(help="Limit the universe to these (repeatable).")
    ] = None,
    as_of: AsOf = None,
    as_json: AsJson = False,
) -> None:
    """Run a YAML rule file over the stored securities; skipped rules are reported, not passed."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        try:
            text = rules.read_text()
        except OSError as e:
            raise NiveshError(f"cannot read the rule file {rules}: {e.strerror}") from None
        rule_set = parse_rules(text, max_rules=r.cfg.screen.max_rules)
        unknown = sorted({x.metric for x in rule_set.rules} - REGISTRY.keys())
        if unknown:  # parse_rules rejects these; kept as a clear guard for registry changes
            raise NiveshError(f"unknown metric(s): {', '.join(unknown)}")
        ids, basis = svc.universe_ids(r.duck, r.sql, security or [])
        needs = inputs_needed(x.metric for x in rule_set.rules)
        universe = load_screen_inputs(r.duck, r.sql, ids, day, r.cfg, needs=needs)
        try:
            found = screen(universe, rule_set, as_of=day, cfg=r.cfg)
        except ValueError as e:
            raise NiveshError(str(e)) from None
    emit("screen", rule_set.name, day, {"screen": found, "requested_basis": basis}, as_json)


@engine_app.command("score")
def score_cmd(
    ctx: typer.Context,
    security: Annotated[list[str] | None, typer.Argument(help="Securities (default: all).")] = None,
    horizon: Annotated[str, typer.Option(help="long_term or positional.")] = "long_term",
    as_of: AsOf = None,
    as_json: AsJson = False,
) -> None:
    """Composite 0-100 score by factor, band and cap, ranked within market and sector."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        rep = svc.score_report(r.duck, r.sql, r.settings, security or [], horizon, day)
    emit(
        "score", None, day,
        {"horizon": horizon, "universe_basis": rep.basis, "scores": rep.scores}, as_json,
    )  # fmt: skip
