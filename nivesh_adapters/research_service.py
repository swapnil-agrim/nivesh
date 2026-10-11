"""Glue between the stored-data engines and the report templates for `/ta`, `/fa` and
`/research` (ST-10.3). Read-only: the caller owns the run, the saving and the check."""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import duckdb

from nivesh_adapters import analysis_service as svc
from nivesh_adapters.analysis_data import USDINR, load_bars
from nivesh_adapters.report import Report
from nivesh_adapters.report_templates import (
    EngineNoteInput,
    ResearchInput,
    engine_note,
    research_note,
)
from nivesh_agents.analysts import Target
from nivesh_agents.committee import SecurityResult
from nivesh_agents.runtime import AgentResult
from nivesh_agents.schemas import AnalystView
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.market_store import get_macro
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_engine.scoring import ScoreCard

KINDS = {"ta": svc.ta_report, "fa": svc.fa_report}


def engine_report(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, kind: str,
    query: str, day: date,
) -> tuple[svc.SecReport, SecurityRow]:  # fmt: skip
    """The engine result for one security and its master row (no model, no spend)."""
    if kind not in KINDS:
        raise NiveshError(f"no engine note for {kind!r}")
    rep = KINDS[kind](duck, sql, settings, query, day)
    row = SecurityMaster(sql).get(rep.security_id)
    if row is None:
        raise NiveshError(f"security {rep.security_id} is not in the security master")
    return rep, row


def note_of(
    rep: svc.SecReport, row: SecurityRow, kind: str, day: date, run_at: datetime, run_id: int,
    analyst: tuple[str, ...] = (), extra_gaps: tuple[str, ...] = (),
) -> tuple[Report, dict[str, Any]]:  # fmt: skip
    """The note and its code-produced facts (the engine result itself)."""
    result = svc.plain(rep.result)
    note = engine_note(
        EngineNoteInput(
            kind,
            rep.symbol,
            row.name or rep.symbol,
            day,
            run_at,
            result,
            run_id,
            analyst,
            extra_gaps,
        )
    )
    return note, {"as_of": day, "result": result}


ANALYST_OF = {"ta": "technical", "fa": "fundamental"}


def analyst_target(row: SecurityRow) -> Target:
    return Target(row.id, row.symbol, row.name or row.symbol, row.asset_class or "equity",
                  row.market)  # fmt: skip


def analyst_lines(result: AgentResult) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(view lines, gap lines) from one analyst's result: a failed or empty one is a gap."""
    view = result.output
    if not isinstance(view, AnalystView):
        return (), (f"analyst view unavailable ({result.reason or result.status})",)
    lines = [view.thesis, *(p.claim for p in view.key_points), *(f"Risk: {r}" for r in view.risks)]
    return tuple(lines), tuple(f"analyst: {g}" for g in view.data_gaps)


LOOKBACK_DAYS = 14
FX_MAX_AGE_DAYS = 10


@dataclass(frozen=True)
class ResearchFacts:
    """Read before the committee runs: the security, its latest stored close, the USD to INR
    rate with its date (US names only) and the engine's valuation lines."""

    security: SecurityRow
    close: Decimal | None
    close_date: date | None
    usd_inr: tuple[Decimal, date] | None
    valuation: tuple[str, ...]
    gaps: tuple[str, ...]


def _number(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def valuation_lines(result: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Valuation lines (current multiples, scenario values) and a gap for what is missing."""
    lines: list[str] = []
    gaps: list[str] = []
    for name, m in (result.get("multiples", {}).get("multiples") or {}).items():
        cur = m.get("current") or {}
        if isinstance(cur, dict) and cur.get("available") and cur.get("value") is not None:
            lines.append(f"{name} now {Decimal(str(cur['value'])):.2f}")
        else:
            gaps.append(f"{name}: {cur.get('reason') or 'unavailable'}")
    for name, sc in ((result.get("range") or {}).get("scenarios") or {}).items():
        per = sc.get("per_share") or {}
        if per.get("available") and per.get("value") is not None:
            growth, disc = _number(sc.get("growth_pct")), _number(sc.get("discount_pct"))
            lines.append(
                f"{name} scenario value per share {Decimal(str(per['value'])):.2f} "
                f"(growth {growth}%, discount rate {disc}%)"
            )
    return lines, gaps


def gather_research(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    security_id: int, day: date,
) -> ResearchFacts:  # fmt: skip
    """The facts a research note shows besides the committee's words. Never raises on missing
    data: each miss becomes a gap line."""
    row = SecurityMaster(sql).get(security_id)
    if row is None:
        raise NiveshError(f"security {security_id} is not in the security master")
    gaps: list[str] = []
    bars = load_bars(duck, [security_id], start=day - timedelta(days=LOOKBACK_DAYS), end=day)
    last = (bars.get(security_id) or [])[-1:]
    if not last:
        gaps.append("no stored close within the last two weeks")
    fx: tuple[Decimal, date] | None = None
    if row.currency == "USD":
        obs = get_macro(duck, USDINR, day - timedelta(days=FX_MAX_AGE_DAYS), day)
        if obs:
            fx = (obs[-1][1], obs[-1][0])
        else:
            gaps.append("no recent USD to INR rate stored")
    lines: list[str] = []
    try:
        rep = svc.valuation_report(duck, sql, settings, f"id:{security_id}", day)
        lines, more = valuation_lines(svc.plain(rep.result))
        gaps += more
    except NiveshError as e:
        gaps.append(f"valuation: {e}")
    return ResearchFacts(
        row, last[0].close if last else None, last[0].date if last else None, fx, tuple(lines),
        tuple(gaps),
    )  # fmt: skip


def research_report(
    result: SecurityResult, facts: ResearchFacts, card: ScoreCard | None, run_at: datetime,
    run_id: int,
) -> tuple[Report, dict[str, Any]]:  # fmt: skip
    """The note and its code-produced facts: close, rate, valuation lines and the score card.
    The committee's views, verdict and cases are model text and are not facts."""
    sec = facts.security
    note = research_note(
        ResearchInput(
            result=result, symbol=sec.symbol, name=sec.name or sec.symbol, currency=sec.currency,
            run_at=run_at, card=card, close=facts.close, close_date=facts.close_date,
            usd_inr=facts.usd_inr, valuation=facts.valuation, gaps=facts.gaps, run_id=run_id,
        )
    )  # fmt: skip
    shown: dict[str, Any] = {
        "last_close": facts.close,
        "close_date": facts.close_date,
        "usd_inr": None if facts.usd_inr is None else facts.usd_inr[0],
        "usd_inr_date": None if facts.usd_inr is None else facts.usd_inr[1],
        "valuation": list(facts.valuation),
        "composite": None if card is None else card.composite,
        "factors": {} if card is None else dict(card.factors),
    }
    return note, {"as_of": result.verdict.as_of, "by_security": {str(sec.id): shown}}
