"""Idea generation from the stores (ST-9.2 to ST-9.4), read-only and offline: presets, the screen
over a named universe, the shortlist and the facts the committee is given. Nothing is fetched,
spent or changed here; recording the committee's calls is the CLI's job (`nivesh_core.ledger`).
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from nivesh_adapters import analysis_service as svc
from nivesh_adapters.analysis_data import benchmark_for, load_bars, load_screen_inputs
from nivesh_adapters.report import Report
from nivesh_adapters.report_templates import IdeaEntry, IdeasInput, ideas_report
from nivesh_adapters.universe_service import resolve_universe
from nivesh_agents.schemas import CommitteeVerdict
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.ledger import LedgerEntry
from nivesh_core.profile import Profile
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_engine.metrics import inputs_needed
from nivesh_engine.scoring import ScoreCard
from nivesh_engine.screen import RuleSet, parse_rules, screen
from nivesh_engine.shortlist import Ranking, Scored, Shortlist, shortlist

PRESET_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SCREENS_DIR = "screens"
LOOKBACK_DAYS = 30  # calendar days of bars read for the latest close


def preset_names(config_dir: Path) -> list[str]:
    folder = config_dir / SCREENS_DIR
    return sorted(p.stem for p in folder.glob("*.yaml")) if folder.is_dir() else []


def preset_horizon(name: str) -> str:
    """Scoring horizon of a preset by its name prefix: `lt-` long term, `pos-` positional."""
    return "positional" if name.startswith("pos-") else "long_term"


def load_preset(config_dir: Path, name: str, *, max_rules: int) -> RuleSet:
    """A preset by plain name from `<config>/screens/`. A name is letters, digits and hyphens;
    a path or anything else is refused before any file is touched."""
    available = preset_names(config_dir)
    if not PRESET_NAME.match(name) or name not in available:
        listed = ", ".join(available) or "none found"
        raise NiveshError(f"unknown preset {name!r}; available: {listed}")
    path = config_dir / SCREENS_DIR / f"{name}.yaml"  # built from the validated name only
    try:
        text = path.read_text()
    except OSError as e:
        raise NiveshError(f"cannot read the preset {name}: {e.strerror}") from None
    return parse_rules(text, max_rules=max_rules)


@dataclass(frozen=True)
class MarketIdeas:
    """The shortlist of one market under one preset, with what it was built from."""

    market: str
    preset: str
    horizon: str
    basis: str
    warnings: list[str]
    matches: int
    short: Shortlist
    cards: dict[int, ScoreCard]
    reason: str = ""


def held_ids(sql: sqlite3.Connection) -> set[int]:
    """Security ids of the latest holdings with a positive quantity, both markets."""
    master = SecurityMaster(sql)
    out: set[int] = set()
    for h in latest_holdings(sql):
        if h.quantity <= 0 or h.asset_class == "mf":
            continue
        found = master.by_isin(h.isin) if h.isin else []
        found = found or [s for s in master.by_symbol(h.symbol) if s.exchange == h.exchange]
        out |= {found[0].id} if found else set()
    return out


def shortlist_for(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, rules: RuleSet, market: str, day: date,
) -> MarketIdeas:  # fmt: skip
    """Run `rules` over the named universe of `market`, score the universe once at the preset's
    horizon, and shortlist the matches. No match gives an empty shortlist with the reason."""
    resolved = resolve_universe(duck, sql, settings, profile, market, day)
    cfg = settings.analysis
    stocks = load_screen_inputs(
        duck, sql, resolved.ids, day, cfg, needs=inputs_needed(r.metric for r in rules.rules)
    )
    try:
        found = screen(stocks, rules, as_of=day, cfg=cfg, basis=resolved.basis)
    except ValueError as e:
        raise NiveshError(str(e)) from None
    name = rules.name or ""
    horizon = preset_horizon(name)
    if not found.matches:
        reason = f"no security in the {market} universe matches {name}"
        return MarketIdeas(
            market, name, horizon, resolved.basis, resolved.warnings, 0,
            Shortlist((), ()), {}, reason,
        )  # fmt: skip
    rep = svc.score_report(
        duck, sql, settings, [], horizon, day, named=(resolved.ids, resolved.basis)
    )
    held = held_ids(sql)
    scored = [
        Scored(m.security_id, m.symbol, rep.cards[m.security_id].composite
               if m.security_id in rep.cards else None,
               stocks[m.security_id].sector, m.security_id in held)
        for m in found.matches
    ]  # fmt: skip
    short = shortlist(scored, settings.ideas)
    return MarketIdeas(
        market, name, horizon, resolved.basis, resolved.warnings, len(found.matches), short,
        dict(rep.cards),
    )  # fmt: skip


@dataclass(frozen=True)
class IdeaFacts:
    """Per security, read before the committee runs: the latest stored close and the market
    benchmark level at that date (or why there is none)."""

    last_close: dict[int, Decimal]
    benchmark: dict[int, tuple[Decimal | None, str | None]]
    names: dict[int, SecurityRow]


def gather_facts(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    ids: Sequence[int], day: date,
) -> IdeaFacts:  # fmt: skip
    """Latest close on or before `day` per security, on the same adjusted basis the engines use,
    and the configured benchmark's level then."""
    rows = SecurityMaster(sql).get_many(ids)
    start = day - timedelta(days=LOOKBACK_DAYS)
    bars = load_bars(duck, sorted(rows), start=start, end=day) if rows else {}
    closes = {i: b[-1].close for i, b in bars.items() if b}
    bench_of = {i: benchmark_for(sql, r, settings.analysis) for i, r in rows.items()}
    wanted = sorted({b.security_id for b in bench_of.values() if b.security_id is not None})
    level = load_bars(duck, wanted, start=start, end=day) if wanted else {}
    benchmark: dict[int, tuple[Decimal | None, str | None]] = {}
    for i, ref in bench_of.items():
        series = level.get(ref.security_id or 0) or []
        if ref.security_id is None:
            benchmark[i] = (None, ref.reason or "no benchmark configured")
        elif not series:
            benchmark[i] = (None, f"no stored bars for benchmark {ref.symbol}")
        else:
            benchmark[i] = (series[-1].close, None)
    return IdeaFacts(closes, benchmark, rows)


def _verdict_hash(snapshot: dict[str, Any], security_id: int) -> str:
    inputs = snapshot["engine_outputs"].get(str(security_id), {})
    body = {"as_of": snapshot["as_of"], "engine_outputs": inputs}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def ledger_rows(
    verdicts: Sequence[CommitteeVerdict], *, run_id: int, run_dir: Path, facts: IdeaFacts,
    preset_of: dict[int, str], reported_ids: frozenset[int],
) -> list[LedgerEntry]:  # fmt: skip
    """One ledger row per committee verdict. Where each field comes from: the verdict itself
    (verdict, horizon, conviction, weight, zone, invalidation, review date); `facts` (last close
    and benchmark level, read before the run); the run's saved snapshot (prompt and model
    versions, and a hash of that security's engine outputs); `reported_ids` and `preset_of`."""
    snapshot = json.loads((run_dir / "snapshot.json").read_text())
    out: list[LedgerEntry] = []
    for v in verdicts:
        zone = v.entry_zone
        level, why = facts.benchmark.get(v.security_id, (None, "no benchmark read"))
        out.append(
            LedgerEntry(
                run_id=run_id,
                security_id=v.security_id,
                verdict=v.verdict,
                horizon=v.horizon,
                conviction=v.conviction,
                suggested_weight_pct=v.suggested_weight_pct,
                entry_low=zone.low if zone else None,
                entry_high=zone.high if zone else None,
                entry_currency=zone.ccy if zone else None,
                invalidation=list(v.invalidation),
                review_date=v.review_date,
                last_close=facts.last_close.get(v.security_id),
                benchmark_level=level,
                benchmark_reason=None if level is not None else why,
                input_hash=_verdict_hash(snapshot, v.security_id),
                prompt_versions=dict(snapshot["prompt_versions"]),
                model_versions=dict(snapshot["models"]),
                reported=v.security_id in reported_ids,
                preset=preset_of.get(v.security_id),
            )  # fmt: skip
        )
    return out


def _zone(v: CommitteeVerdict) -> str:
    z = v.entry_zone
    return "none given" if z is None else f"{z.low} to {z.high} {z.ccy}"


def render_idea(
    n: int, v: CommitteeVerdict, row: SecurityRow, label: str, preset: str
) -> list[str]:  # fmt: skip
    """The report block of one idea: thesis, entry zone, invalidation, suggested weight, and
    what would prove it wrong."""
    weight = "none" if v.suggested_weight_pct is None else f"{v.suggested_weight_pct}%"
    head = f"{n}. {row.symbol} ({row.market})  {v.verdict}  conviction {v.conviction}"
    return [
        head + (f"  [{label}]" if label else ""),
        f"   thesis: {v.bull_case}",
        f"   entry zone: {_zone(v)}",
        f"   suggested weight: {weight}",
        f"   invalidation: {'; '.join(v.invalidation) or 'none given'}",
        f"   what would prove this wrong: {v.bear_case}",
        f"   review by {v.review_date}; preset {preset}",
    ]


def idea_report(
    *, run_at: datetime, run_id: int, day: date, preset: str, count: int,
    verdicts: Sequence[CommitteeVerdict], ranking: Ranking, facts: IdeaFacts,
    labels: dict[int, str], cards: dict[int, ScoreCard], notes: Sequence[str],
) -> tuple[Report, dict[str, Any]]:  # fmt: skip
    """The ideas report and the code-produced facts it is checked against: the stored close,
    benchmark level and score card per security. The committee's own words are not facts."""
    by_id = {v.security_id: v for v in verdicts}
    entries = tuple(
        IdeaEntry(i, x.symbol, facts.names[x.security_id].market, labels.get(x.security_id, ""),
                  by_id[x.security_id])
        for i, x in enumerate(ranking.ideas, start=1)
    )  # fmt: skip
    shown: dict[str, Any] = {}
    for v in verdicts:
        card = cards.get(v.security_id)
        shown[str(v.security_id)] = {
            "last_close": facts.last_close.get(v.security_id),
            "benchmark": facts.benchmark.get(v.security_id, (None, None))[0],
            "composite": None if card is None else card.composite,
            "factors": {} if card is None else dict(card.factors),
        }
    report = ideas_report(IdeasInput(
        run_at=run_at, as_of=day, preset=preset, entries=entries, wanted=count,
        qualified=ranking.qualified, message=ranking.message, notes=tuple(notes), run_id=run_id,
    ))  # fmt: skip
    return report, {"as_of": day, "by_security": shown}
