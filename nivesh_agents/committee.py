"""Portfolio-manager orchestrator (ST-7.9): the research committee for one or more securities.

Per security: analysts in parallel, then debate (and lenses in deep mode), then the risk manager,
then the input snapshot and every output are saved, and only then does the portfolio manager
propose a verdict. A proposal never reaches the caller as is: `finalise` alone produces
a verdict, and it applies `verdict_ceiling` and the weight bounds in code. All agents
share one concurrency limit per run. This is a library function; the CLI command is E10.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

from nivesh_adapters import analysis_service as svc
from nivesh_agents.analysts import QUICK_ANALYSTS, Target, macro_once, run_analyst
from nivesh_agents.context import RunCtx
from nivesh_agents.debate import Debate, run_debate, views_payload
from nivesh_agents.lenses import Lenses, disagreement_count, run_lenses
from nivesh_agents.prompts import load_prompt
from nivesh_agents.risk_manager import merge_assessment, run_risk
from nivesh_agents.runtime import AgentResult, ToolCtx
from nivesh_agents.schemas import CommitteeVerdict, RiskAssessment
from nivesh_agents.specs import SPECS
from nivesh_agents.store import RunStore
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.config import Price, Settings
from nivesh_core.profile import Profile
from nivesh_core.trace import Tracer, digest_text
from nivesh_engine.committee_rules import (
    INSUFFICIENT_BAND,
    UPWARD,
    Bounds,
    RiskFacts,
    below_minimum,
    coverage_pct,
    final_weight,
    risk_veto,
    verdict_ceiling,
    weight_bounds,
)
from nivesh_engine.scoring import ScoreCard

TIERS = ("quick", "deep")
FUND_BAND = "fund"
NOT_RUN = "not_run_insufficient_data"
D = Decimal


@dataclass(frozen=True)
class SecurityInput:
    """One security with the facts computed in code: the score card (None for a fund) and the
    risk facts. Models never produce either."""

    target: Target
    card: ScoreCard | None
    facts: RiskFacts


@dataclass(frozen=True)
class CommitteeInputs:
    securities: tuple[SecurityInput, ...]
    as_of: date
    profile_limits: dict[str, str]


@dataclass
class SecurityResult:
    security_id: int
    verdict: CommitteeVerdict
    views: dict[str, BaseModel]
    debate: Debate | None
    lenses: Lenses | None
    risk: RiskAssessment
    disagreement: int | None
    coverage: Decimal
    failures: list[str] = field(default_factory=list)


@dataclass
class CommitteeResult:
    results: list[SecurityResult] = field(default_factory=list)
    stage_ms: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def verdicts(self) -> list[CommitteeVerdict]:
        return [r.verdict for r in self.results]


def prepare_inputs(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, queries: list[str], day: date, *, starter_weight_pct: Decimal,
    cards: Mapping[int, ScoreCard] | None = None,
) -> CommitteeInputs:  # fmt: skip
    """Resolve the securities and compute the score cards and risk facts in code. `cards`
    (by security id) are score cards already computed over a universe: nothing is scored again,
    and a security without one gets an empty card, so its coverage reads as a data gap. A query
    may be "id:<n>" to name one security exactly."""
    items: list[SecurityInput] = []
    for q in queries:
        sec = svc.resolve_security(sql, q)
        facts = svc.risk_facts(
            duck, sql, settings, profile, q, day, starter_weight_pct=starter_weight_pct
        )
        if sec.asset_class == "mf":
            row = sql.execute("SELECT amfi_code FROM security WHERE id = ?", (sec.id,)).fetchone()
            target = Target(sec.id, sec.symbol, sec.name or sec.symbol, "mf", sec.market,
                            scheme=str(row[0]) if row and row[0] else None)  # fmt: skip
            items.append(SecurityInput(target, None, facts))
            continue
        if cards is None:
            card = svc.score_one(duck, sql, settings, q, "long_term", day).card
        else:
            card = cards.get(sec.id) or ScoreCard(sec.id, "long_term")  # empty: a data gap
        target = Target(sec.id, sec.symbol, sec.name or sec.symbol, sec.asset_class or "equity",
                        sec.market)  # fmt: skip
        items.append(SecurityInput(target, card, facts))
    limits = {
        "max_position_pct": format(D(str(profile.max_position_pct)).normalize(), "f"),
        "max_sector_pct": format(D(str(profile.max_sector_pct)).normalize(), "f"),
    }
    return CommitteeInputs(tuple(items), day, limits)


def snapshot_payload(
    inputs: CommitteeInputs, tier: str, cfg: AgentsSettings, settings: Settings, run_id: int
) -> dict[str, Any]:
    """Everything the verdicts are derived from, as plain data."""
    versions = {n: f"v{load_prompt(n, pins=cfg.prompt_pins).version}" for n in sorted(SPECS)}
    models = {n: getattr(cfg.models, cfg.tiers[s.role]) for n, s in sorted(SPECS.items())}
    scoring = settings.analysis.scoring
    agents_digest = hashlib.sha256(
        json.dumps(cfg.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "run_id": run_id, "as_of": inputs.as_of.isoformat(), "tier": tier,
        "securities": [
            {"security_id": s.target.security_id, "symbol": s.target.symbol, "kind": s.target.kind}
            for s in inputs.securities
        ],
        "profile_limits": dict(inputs.profile_limits),
        "engine_outputs": {
            str(s.target.security_id): {"risk_facts": svc.plain(s.facts),
                                        "score_card": svc.plain(s.card)}  # fmt: skip
            for s in inputs.securities
        },
        "coverage": {
            str(s.target.security_id): {
                "inputs_available": s.card.inputs_available if s.card else 1,
                "inputs_total": s.card.inputs_total if s.card else 1,
            }
            for s in inputs.securities
        },
        "prompt_versions": versions, "models": models,
        "config_digest": {
            "agents": digest_text(agents_digest), "weights_version": scoring.weights_version,
            "weights_digest": digest_text(scoring.weights_digest()),
        },
    }  # fmt: skip


def finalise(
    proposal: CommitteeVerdict | None, sec: SecurityInput, as_of: date, coverage: Decimal,
    veto: bool, bounds: Bounds, min_coverage: Decimal, notes: list[str],
) -> CommitteeVerdict:  # fmt: skip
    """The sole producer of a committee verdict. The model's proposal (or a safe default
    when there is none) is lowered by `verdict_ceiling` and its weight is clamped by the bounds;
    the veto flag, the coverage and the list of changes are set by code."""
    base = proposal or CommitteeVerdict(
        security_id=sec.target.security_id, as_of=as_of, verdict="INSUFFICIENT_DATA",
        horizon="long_term_1y_plus", conviction="low", review_date=as_of + timedelta(days=30),
        bull_case="Not available.", bear_case="Not available.", risk_notes="Not available.",
        vetoed_by_risk=False, coverage_pct=D(0),
    )  # fmt: skip
    card = sec.card
    ceiling = verdict_ceiling(
        base.verdict, veto=veto, card_band=card.band if card else FUND_BAND,
        card_cap=card.cap if card else None, hard_flag=sec.facts.hard_flag, coverage=coverage,
        min_coverage=min_coverage,
    )  # fmt: skip
    lowered = veto and base.verdict in UPWARD and ceiling.verdict == "HOLD"
    weight, weight_notes = final_weight(
        ceiling.verdict, base.suggested_weight_pct, bounds, lowered_by_veto=lowered
    )
    none = ceiling.verdict == "INSUFFICIENT_DATA"
    return base.model_copy(
        update={
            "verdict": ceiling.verdict, "vetoed_by_risk": ceiling.vetoed_by_risk,
            "suggested_weight_pct": weight, "entry_zone": None if none else base.entry_zone,
            "conviction": "low" if none else base.conviction,
            "coverage_pct": coverage.quantize(D("0.01")),
            "overrides": [*notes, *ceiling.overrides, *weight_notes],
        }
    )  # fmt: skip


def _pm_prompt(
    sec: SecurityInput, as_of: date, mode: str, coverage: Decimal, views: dict[str, BaseModel],
    debate: Debate | None, risk: RiskAssessment, lenses: Lenses | None, failures: list[str],
) -> str:  # fmt: skip
    body: dict[str, Any] = {
        "task": "Propose the verdict from these inputs only. Use no tools.",
        "as_of": as_of.isoformat(), "mode": mode, "security_id": sec.target.security_id,
        "symbol": sec.target.symbol, "coverage_pct": str(coverage.quantize(D("0.01"))),
        "views": views_payload(views), "risk": risk.model_dump(mode="json"),
        "score_card": svc.plain(sec.card), "failures": failures,
        "debate": None if debate is None else {
            "turns": [t.model_dump(mode="json") for t in debate.turns],
            "strongest_bull": debate.strongest("bull"),
            "strongest_bear": debate.strongest("bear"), "partial": debate.partial,
        },
        "lenses": None if lenses is None else [v.model_dump(mode="json") for v in lenses.views],
    }  # fmt: skip
    return json.dumps(body, sort_keys=True)


class _Committee:
    def __init__(
        self, run: RunCtx, store: RunStore, inputs: CommitteeInputs, clock: Callable[[], float],
        stage_ms: dict[str, int],
    ) -> None:  # fmt: skip
        self.run, self.store, self.inputs, self.clock, self.stage_ms = (
            run,
            store,
            inputs,
            clock,
            stage_ms,
        )
        self.macro_saved = False

    async def timed(self, stage: str, aw: Any) -> Any:
        t0 = self.clock()
        out = await aw
        self.stage_ms[stage] += round((self.clock() - t0) * 1000)
        return out

    def _persist(
        self, sec: SecurityInput, results: dict[str, AgentResult | BaseException],
        debate: Debate | None, lenses: Lenses | None, risk: RiskAssessment,
    ) -> None:  # fmt: skip
        t0, sid = self.clock(), sec.target.security_id
        for name, res in results.items():
            macro = name == "macro"
            if macro and self.macro_saved:
                continue
            self.macro_saved = self.macro_saved or macro
            who = None if macro else sid
            if isinstance(res, BaseException):
                self.store.failed(name, who, type(res).__name__)
            elif isinstance(res.output, BaseModel):
                self.store.output(name, who, res.output.model_dump(mode="json"))
            else:
                self.store.failed(name, who, res.reason or "failed")
        if debate is not None:
            self.store.output("debate", sid, {
                "turns": [t.model_dump(mode="json") for t in debate.turns],
                "failures": debate.failures, "partial": debate.partial,
            })  # fmt: skip
        if lenses is not None and (lenses.views or lenses.failures):
            self.store.output("lenses", sid, {
                "views": [v.model_dump(mode="json") for v in lenses.views],
                "failures": lenses.failures,
            })  # fmt: skip
        self.store.output("risk", sid, risk.model_dump(mode="json"))
        self.stage_ms["persist"] += round((self.clock() - t0) * 1000)

    async def process(self, sec: SecurityInput) -> SecurityResult:
        run, cfg = self.run, self.run.cfg
        t, deep = sec.target, self.run.mode == "deep"
        fund = t.kind == "mf"
        names = ["mf"] if fund else list(QUICK_ANALYSTS)
        coros = [run_analyst(run, n, t) for n in names]
        if deep and not fund:
            names.append("macro")
            coros.append(macro_once(run))
        raw = await self.timed("analysts", asyncio.gather(*coros, return_exceptions=True))
        results: dict[str, AgentResult | BaseException] = dict(zip(names, raw, strict=True))
        views: dict[str, BaseModel] = {}
        failures: list[str] = []
        for name, res in results.items():
            if isinstance(res, BaseException):
                failures.append(f"{name}: {type(res).__name__}")
            elif isinstance(res.output, BaseModel):
                views[name] = res.output
            else:
                failures.append(f"{name}: {res.reason}")
        card = sec.card
        coverage = coverage_pct(
            card.inputs_available if card else 1, card.inputs_total if card else 1,
            len(views), len(names),
        )  # fmt: skip
        veto = risk_veto(sec.facts, max_days_to_trade=cfg.max_days_to_trade)
        bounds = weight_bounds(sec.facts)
        stop = (card is not None and card.band == INSUFFICIENT_BAND) or below_minimum(
            coverage, cfg.min_coverage_pct
        )
        debate: Debate | None = None
        lenses: Lenses | None = None
        if stop:  # the verdict is forced to INSUFFICIENT_DATA: no further agent is called
            risk = merge_assessment(
                None, sec.facts, veto, bounds, reason=NOT_RUN, fail_closed=False
            )
            risk = risk.model_copy(update={"security_id": t.security_id})
        else:
            if not fund:
                debate, lenses = await self.timed(
                    "debate",
                    asyncio.gather(run_debate(run, t, views), run_lenses(run, t, views)),
                )
            outcome = await self.timed(
                "risk", run_risk(run, t, sec.facts, max_days_to_trade=cfg.max_days_to_trade)
            )
            risk, veto = outcome.assessment, outcome.veto
        self._persist(sec, results, debate, lenses, risk)
        proposal: CommitteeVerdict | None = None
        notes: list[str] = []
        if stop:
            why = (
                "score card has insufficient data"
                if card is not None and card.band == INSUFFICIENT_BAND
                else f"coverage {coverage.quantize(D('0.01'))} is below the minimum "
                f"{cfg.min_coverage_pct}"
            )
            notes.append(f"portfolio manager not called: {why}")
        else:
            pm = await self.timed(
                "pm", self._pm(sec, coverage, views, debate, risk, lenses, failures)
            )
            if isinstance(pm.output, CommitteeVerdict):
                proposal = pm.output
            else:
                notes.append(f"pm_unavailable: {pm.reason}")
                failures.append(f"pm: {pm.reason}")
        verdict = finalise(
            proposal, sec, self.inputs.as_of, coverage, veto.veto,
            bounds, cfg.min_coverage_pct, notes,
        )  # fmt: skip
        self.store.output("verdict", t.security_id, verdict.model_dump(mode="json"))
        count = None
        if lenses is not None and lenses.views:
            count = disagreement_count(lenses.views, verdict.verdict)
        return SecurityResult(
            t.security_id, verdict, views, debate, lenses, risk, count, coverage, failures
        )  # fmt: skip

    async def _pm(
        self, sec: SecurityInput, coverage: Decimal, views: dict[str, BaseModel],
        debate: Debate | None, risk: RiskAssessment, lenses: Lenses | None, failures: list[str],
    ) -> AgentResult:  # fmt: skip
        as_of, sid = self.inputs.as_of, sec.target.security_id

        def check(model: BaseModel, ctx: ToolCtx) -> list[str]:
            if not isinstance(model, CommitteeVerdict):
                return ["unexpected output type"]
            errors = []
            if model.security_id != sid:
                errors.append(f"security_id must be {sid}")
            if model.as_of != as_of:
                errors.append(f"as_of must be {as_of.isoformat()}")
            if model.review_date <= as_of:
                errors.append("review_date must be after as_of")
            if model.verdict in UPWARD and not model.invalidation:
                errors.append("an upward verdict needs at least one invalidation item")
            return errors

        prompt = _pm_prompt(
            sec, as_of, self.run.mode, coverage, views, debate, risk, lenses, failures
        )
        return await self.run.run(
            SPECS["pm"], prompt, security_id=sid, extra_check=check, evidence_ids=frozenset()
        )


async def run_committee(
    inputs: CommitteeInputs,
    *,
    tier: str,
    cfg: AgentsSettings,
    settings: Settings,
    tracer: Tracer,
    run_dir: Path,
    run_id: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    prices: dict[str, Price] | None = None,
    usd_inr: float = 90.0,
    env: dict[str, str] | None = None,
) -> CommitteeResult:
    """Run the committee. `tier` is quick or deep (the cost gate decides it upstream; brief is
    not a committee tier). Raises before any agent call when the run directory is missing."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}, got {tier!r}")
    store = RunStore(run_dir, tracer)
    run = RunCtx(cfg, tracer, inputs.as_of, tier, prices or {}, usd_inr, env or {})  # type: ignore[arg-type]
    store.snapshot(snapshot_payload(inputs, tier, cfg, settings, run_id or tracer.run_id))
    stage_ms: dict[str, int] = defaultdict(int)
    committee = _Committee(run, store, inputs, clock, stage_ms)
    done = await asyncio.gather(
        *(committee.process(s) for s in inputs.securities), return_exceptions=True
    )
    out = CommitteeResult()
    for sec, res in zip(inputs.securities, done, strict=True):
        if isinstance(res, SecurityResult):
            out.results.append(res)
            continue
        reason = f"{sec.target.symbol}: {type(res).__name__}"  # a bug, not an agent failure
        out.failures.append(reason)
        bounds = weight_bounds(sec.facts)
        v = finalise(None, sec, inputs.as_of, D(0), True, bounds, cfg.min_coverage_pct, [reason])
        risk = merge_assessment(None, sec.facts, risk_veto(sec.facts), bounds)
        out.results.append(
            SecurityResult(sec.target.security_id, v, {}, None, None, risk, None, D(0), [reason])
        )
    for r in out.results:
        out.failures += [f"{r.verdict.security_id}: {f}" for f in r.failures]
    out.stage_ms = dict(stage_ms)
    tracer.finish_run(getattr(cfg.models, cfg.tiers["pm"]))
    return out
