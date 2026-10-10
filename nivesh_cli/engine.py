"""`nivesh ta | fa | valuation | flags | xray | risk | screen | score` (E6 analysis engines).

Thin read-only commands over the stored data and the pure engines in `nivesh_engine`. Nothing is
fetched and nothing is written: both stores are opened for reading only. Every command takes
`--as-of` (default: today in India) and `--json`, which prints exact decimal strings.
"""

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import duckdb
import typer

from nivesh_adapters.analysis_data import (
    load_flag_inputs,
    load_peer_multiples,
    load_risk_inputs,
    load_screen_inputs,
    load_valuation_inputs,
    load_xray_inputs,
)
from nivesh_cli.common import settings_of, user_errors
from nivesh_cli.market import resolve_security
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.profile import Profile
from nivesh_core.security_master import SecurityMaster
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.metrics import REGISTRY, Bundles, inputs_needed
from nivesh_engine.redflags import detect_flags
from nivesh_engine.risk import risk_metrics
from nivesh_engine.scoring import ranking, raw_inputs, score_universe
from nivesh_engine.screen import parse_rules, screen
from nivesh_engine.valuation import valuation_multiples, valuation_range
from nivesh_engine.xray import portfolio_xray

engine_app = typer.Typer()

AsOf = Annotated[str | None, typer.Option("--as-of", help="Date, YYYY-MM-DD (default: today).")]
AsJson = Annotated[bool, typer.Option("--json", help="Print exact decimal strings as JSON.")]
Symbol = Annotated[str, typer.Argument(help="ISIN, symbol or company name.")]
RENAMED = {"key": "ref", "portfolio_vol": "overall_vol"}
HORIZONS = ("long_term", "positional")
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
def plain(obj: object) -> Any:
    """A JSON-ready copy: decimals as exact strings, dates as ISO text, dataclasses as mappings."""
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        # `key` (a holding's row id) and `portfolio_vol` contain words a redaction pass keyed on
        # field names (nivesh_core.redact) blanks; they are renamed at this boundary
        return {RENAMED.get(f.name, f.name): plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, set | frozenset):
        return sorted((plain(v) for v in obj), key=str)
    if isinstance(obj, Sequence):
        return [plain(v) for v in obj]
    return str(obj)


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
def _inputs(r: Reader, query: str, day: date, needs: set[str]) -> Any:
    sec = resolve_security(r.sql, query)
    found = load_screen_inputs(r.duck, r.sql, [sec.id], day, r.cfg, needs=needs)
    return sec, found[sec.id]


@engine_app.command("ta")
def ta_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Technical indicators, support and resistance, and the setup, from stored daily bars."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        sec, inp = _inputs(r, security, day, {"bars", "benchmark", "sector_bars"})
        b = Bundles(inp, day, r.cfg)
        result = {"indicators": b.ta(), "setup": b.setup()}
    emit("ta", sec.symbol, day, result, as_json)


@engine_app.command("fa")
def fa_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Growth, profitability, balance sheet and cash quality, using filings up to the date."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        sec, inp = _inputs(r, security, day, {"statements", "shareholding"})
        result = Bundles(inp, day, r.cfg).fa()
    emit("fa", sec.symbol, day, result, as_json)


@engine_app.command("valuation")
def valuation_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Multiples with history, percentile and peer median, plus the reverse-DCF fair-value range."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        sec = resolve_security(r.sql, security)
        inputs = load_valuation_inputs(r.duck, r.sql, [sec.id], day, r.cfg)[sec.id]
        peers = load_peer_multiples(r.duck, r.sql, sec.id, day, r.cfg)
        result = {
            "multiples": valuation_multiples(inputs, peers, cfg=r.cfg),
            "range": valuation_range(inputs, cfg=r.cfg),
        }
    emit("valuation", sec.symbol, day, result, as_json)


@engine_app.command("flags")
def flags_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """Red flags with status, severity and evidence; a flag that cannot be tested says so."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        sec = resolve_security(r.sql, security)
        inputs = load_flag_inputs(r.duck, r.sql, sec.id, day, r.cfg)
        if inputs is None:
            raise NiveshError(f"{security!r} is not in the security master")
        result = {"flags": detect_flags(inputs, as_of=day, cfg=r.cfg)}
    emit("flags", sec.symbol, day, result, as_json)


# ---- the portfolio commands ---------------------------------------------------------------------
@engine_app.command("xray")
def xray_cmd(ctx: typer.Context, as_of: AsOf = None, as_json: AsJson = False) -> None:
    """Allocation, drift, concentration, limits, look-through and XIRR of the stored holdings."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        inp = load_xray_inputs(r.duck, r.sql, r.settings, day)
        xray = portfolio_xray(
            inp.rows, r.profile, sector_of=inp.sector_of, market_cap_of=inp.market_cap_of,
            mf_category_of=inp.mf_category_of, flows=inp.flows, look_through=inp.look_through,
            cfg=r.cfg.xray, market_of=inp.market_of,
        )  # fmt: skip
        result = {"xray": xray, "notes": inp.notes}
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
        if (candidate is None) != (weight is None):
            raise NiveshError("--candidate and --weight go together")
        pct = None
        if weight is not None:
            try:
                pct = Decimal(weight)
            except ArithmeticError:
                raise NiveshError(f"--weight must be a number, got {weight!r}") from None
            if not pct.is_finite() or not Decimal(0) < pct < Decimal(100):
                raise NiveshError("--weight must be between 0 and 100 (percent)")
        try:
            inp = load_risk_inputs(
                r.duck, r.sql, r.settings, day, candidate=candidate, proposed_weight_pct=pct
            )
        except ValueError as e:
            raise NiveshError(str(e)) from None
        res = risk_metrics(
            inp.holdings, inp.candidate, inp.bars, inp.benchmarks, cfg=r.cfg.risk, as_of=day,
            max_position_pct=Decimal(str(r.profile.max_position_pct)),
            max_sector_pct=Decimal(str(r.profile.max_sector_pct)),
        )  # fmt: skip
        result = {"risk": res, "notes": inp.notes}
    emit("risk", candidate, day, result, as_json)


# ---- the universe commands ----------------------------------------------------------------------
def _universe_ids(r: Reader, symbols: Sequence[str]) -> tuple[list[int], str]:
    """The securities to read: the ones named, else every direct security with stored bars."""
    if symbols:
        ids = sorted({resolve_security(r.sql, s).id for s in symbols})
        return ids, f"explicit list of {len(ids)} securities named on the command line"
    rows = r.duck.execute("SELECT DISTINCT security_id FROM price_bar").fetchall()
    stored = [int(x[0]) for x in rows]
    found = SecurityMaster(r.sql).get_many(stored)
    ids = sorted(i for i, s in found.items() if s.asset_class not in ("mf", "index"))
    return (
        ids,
        f"all {len(ids)} securities with stored bars (no investable universe is defined yet)",
    )


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
        ids, basis = _universe_ids(r, security or [])
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
        if horizon not in HORIZONS:
            raise NiveshError(f"--horizon must be one of {', '.join(HORIZONS)}")
        ids, basis = _universe_ids(r, security or [])
        needs = {"bars", "benchmark", "sector_bars", "statements", "shareholding", "closes",
                 "last_close", "estimates", "filings", "peers"}  # fmt: skip
        universe = load_screen_inputs(r.duck, r.sql, ids, day, r.cfg, needs=needs)
        raw, meta, flags = raw_inputs(universe, as_of=day, cfg=r.cfg)
        cards = score_universe(raw, meta, flags, horizon=horizon, cfg=r.cfg)  # type: ignore[arg-type]
        scored = [{"symbol": universe[i].symbol, "card": cards[i]} for i in ranking(cards)]
    emit(
        "score", None, day, {"horizon": horizon, "universe_basis": basis, "scores": scored}, as_json
    )
