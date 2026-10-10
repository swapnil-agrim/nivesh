"""Synthetic payload builders for the committee tests. Everything here is invented: no real
securities, no holdings, no PII. Tool-use ids are built by concatenation."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from nivesh_agents.analysts import Target
from nivesh_agents.context import RunCtx
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.trace import Tracer
from nivesh_engine.committee_rules import RiskFacts


def tid(n: int) -> str:
    return "toolu_" + f"{n:04d}"


def ev(
    n: int = 1, field: str = "roce", value: Any = 18.2, as_of: str = "2026-01-02"
) -> dict[str, Any]:
    return {"tool_call_id": tid(n), "field": field, "value": value, "as_of": as_of}


def point(n: int = 1, claim: str = "Return on capital is strong") -> dict[str, Any]:
    return {"claim": claim, "evidence": [ev(n)]}


def analyst(agent: str = "fundamental", **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "agent": agent, "security_id": 1, "as_of": "2026-01-02",
        "stance": "bullish", "score": 70, "confidence": "medium",
        "thesis": "A short synthetic thesis.",
        "key_points": [point(1), point(2), point(3)], "risks": ["cycle"], "data_gaps": [],
    }  # fmt: skip
    d.update(over)
    return d


def macro(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "agent": "macro", "as_of": "2026-01-02", "regime": "neutral",
        "score": 50, "confidence": "low", "thesis": "Mixed signals.", "key_points": [point(1)],
        "sector_tilts": [{"sector": "IT", "tilt": "neutral", "evidence": [ev(1)]}],
        "risks": [], "data_gaps": [],
    }  # fmt: skip
    d.update(over)
    return d


def fund(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "agent": "mf", "security_id": 5, "scheme": "100001",
        "as_of": "2026-01-02", "stance": "neutral", "score": 55, "confidence": "medium",
        "thesis": "Reasonable fund.", "key_points": [point(1)], "cost_note": "TER ok",
        "overlap_note": "low", "category_fit": "fits", "action": "keep", "switch_target": None,
        "risks": [], "data_gaps": [],
    }  # fmt: skip
    d.update(over)
    return d


def turn(side: str = "bull", rnd: int = 1, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "security_id": 1, "round": rnd, "side": side,
        "claims": [{"claim": "Cash conversion is steady", "evidence": [
            {"view": "fundamental", "point_index": 0}]}],
        "rebuts": [], "strongest_unrebutted": None, "summary": False,
    }  # fmt: skip
    d.update(over)
    return d


def lens(name: str = "value", decision: str = "would_buy", **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "lens": name, "security_id": 1, "decision": decision,
        "paragraph": "A short synthetic paragraph.",
        "evidence": [{"view": "fundamental", "point_index": 0}],
    }  # fmt: skip
    d.update(over)
    return d


def risk(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "security_id": 1, "veto": False, "veto_reasons": [],
        "weight_bounds": {"min_pct": 0, "max_pct": 5}, "liquidity_note": "ok",
        "correlation_note": "ok", "concentration_note": "ok", "risks": [], "overrides": [],
    }  # fmt: skip
    d.update(over)
    return d


def crit_check(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"criterion_id": 1, "status": "not_met", "evidence": [ev(1)]}
    d.update(over)
    return d


def review(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 2, "security_id": 1, "as_of": "2026-01-02", "action": "HOLD",
        "confidence": "medium", "reasons": [{"code": "thesis_intact", "text": "Returns hold up"}],
        "criteria": [crit_check()], "thesis_status": "intact", "valuation_stretch": False,
        "override_reason": "", "evidence": [ev(1)],
    }  # fmt: skip
    d.update(over)
    return d


def kill(n: int = 1, machine: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {"criterion_id": n, "text": f"Synthetic criterion {n} breaks"}
    if machine:
        d.update(metric="roce_pct", comparator="lt", threshold="12.5", unit="%")
    return d


def thesis_draft(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "security_id": 1, "as_of": "2026-01-02",
        "horizon": "long_term_1y_plus", "why": "Durable franchise with steady returns.",
        "kill_criteria": [kill(1), kill(2, machine=False)],
        "evidence": [{"view": "fundamental", "point_index": 0}], "data_gaps": [],
    }  # fmt: skip
    d.update(over)
    return d


def verdict(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": 1, "security_id": 1, "as_of": "2026-01-02", "verdict": "HOLD",
        "horizon": "long_term_1y_plus", "conviction": "medium", "suggested_weight_pct": None,
        "entry_zone": {"low": 100, "high": 110, "ccy": "INR"},
        "invalidation": ["Margins fall two quarters in a row"], "review_date": "2026-04-02",
        "bull_case": "b", "bear_case": "r", "risk_notes": "n", "vetoed_by_risk": False,
        "coverage_pct": 100, "overrides": [],
    }  # fmt: skip
    d.update(over)
    return d


# ---- runners ---------------------------------------------------------------------------------
TA_TOOL = "mcp__engine__ta_compute"
SETUP: dict[str, Any] = {
    "as_of": "2026-01-02", "setup": "trend_continuation", "reason": None,
    "matched": ["trend_continuation"], "entry_low": 103.55817388, "entry_high": 103.82,
    "stop": 102.51086942, "invalidation": 101.54772727, "invalidation_basis": "support",
}  # fmt: skip
NO_SETUP: dict[str, Any] = {**SETUP, "setup": "none", "entry_low": None, "entry_high": None,
                            "stop": None, "invalidation": None}  # fmt: skip


def ta_content(setup: dict[str, Any] | None = None) -> str:
    result = {"indicators": {"values": {}}, "setup": SETUP if setup is None else setup}
    return json.dumps({"data": {"subject": "US1", "result": result}, "as_of": "2026-01-02",
                       "source": "nivesh-engine", "stale": False})  # fmt: skip


def ta_call(
    n: int = 1, setup: dict[str, Any] | None = None
) -> tuple[str, str, dict[str, Any], Any]:
    return (tid(n), TA_TOOL, {"security": "US1"}, ta_content(setup))


def calls(
    tool: str, *ns: int, content: Any = "result"
) -> tuple[tuple[str, str, dict[str, Any], Any], ...]:
    """Tool calls `(id, tool, input, result)` for the given id numbers."""
    return tuple((tid(n), tool, {"security": "US1"}, content) for n in ns)


def levels(setup: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    s = SETUP if setup is None else setup
    d = {"setup_type": s["setup"], "entry_low": s["entry_low"], "entry_high": s["entry_high"],
         "stop": s["stop"], "invalidation": s["invalidation"]}  # fmt: skip
    d.update(over)
    return d


AS_OF = date(2026, 1, 2)


def run_ctx(tmp_path: Path, mode: str = "quick", cfg: AgentsSettings | None = None) -> RunCtx:
    return RunCtx(cfg or AgentsSettings(), Tracer(tmp_path, 1), AS_OF, mode)  # type: ignore[arg-type]


TARGET = Target(1, "US1", "Example Tech 1")


def facts(**over: Any) -> RiskFacts:
    """Risk facts with no cause for a veto: position limit 10, sector limit 30, 20 headroom."""
    base: dict[str, Any] = {
        "excluded": False, "hard_flag": False, "position_over_limit": False,
        "sector_over_limit": False, "days_to_trade": None, "max_position_pct": Decimal(10),
        "max_sector_pct": Decimal(30), "sector_headroom_pct": Decimal(20),
        "tested_weight_pct": Decimal(2),
    }  # fmt: skip
    base.update(over)
    return RiskFacts(**base)


def views(*, with_levels: bool = False) -> dict[str, Any]:
    """Three validated analyst views (key points 0..2 on each)."""
    from nivesh_agents.schemas import AnalystView

    out = {}
    for agent in ("fundamental", "technical", "news"):
        out[agent] = AnalystView.model_validate_json(
            json.dumps(analyst(agent, key_points=[point(1), point(2), point(3)]))
        )
    return out


def register_analyst_calls(run: RunCtx, *agents: str) -> None:
    """Record tool calls for these analysts so their ids are valid evidence for debate agents."""
    for a in agents:
        for n in (1, 2, 3):
            run.tracer.tool_call(tid(n), "mcp__x__y", {}, agent=a, security_id=1)


# ---- a scripted committee --------------------------------------------------------------------
def card(n: int = 1, **over: Any) -> Any:
    from nivesh_engine.scoring import ScoreCard

    base: dict[str, Any] = {
        "security_id": n, "horizon": "long_term", "composite": Decimal(70), "band": "upper",
        "cap": None, "inputs_available": 10, "inputs_total": 10,
    }  # fmt: skip
    base.update(over)
    return ScoreCard(**base)


def sec_input(n: int = 1, *, facts_over: dict[str, Any] | None = None, **card_over: Any) -> Any:
    from nivesh_agents.committee import SecurityInput

    return SecurityInput(
        Target(n, f"S{n:03d}", f"Example {n}"), card(n, **card_over), facts(**(facts_over or {}))
    )


def fund_input(n: int = 5, **facts_over: Any) -> Any:
    from nivesh_agents.committee import SecurityInput

    return SecurityInput(
        Target(n, "INF000A01011", "Example Fund", kind="mf", scheme="100001"),
        None,
        facts(**facts_over),
    )


def committee_inputs(*secs: Any) -> Any:
    from nivesh_agents.committee import CommitteeInputs

    return CommitteeInputs(tuple(secs), AS_OF, {"max_position_pct": "10", "max_sector_pct": "30"})


def _sid(call: Any) -> int:
    body = json.loads(call.prompt)
    return int(body["security_id"] if "security_id" in body else body["security"]["security_id"])


def happy(**replace: Any) -> Any:
    """A fake SDK whose every agent answers validly (the PM proposes an upward verdict).
    Pass `agent=callable_or_attempts` to replace one agent."""
    from tests.agents.fake_sdk import reply, script

    def fa(call: Any) -> Any:
        return reply(
            analyst("fundamental", security_id=_sid(call)),
            tools=calls("mcp__engine__fa_compute", 1, 2, 3),
        )

    def ta(call: Any) -> Any:
        v = analyst("technical", security_id=_sid(call), key_points=[point(1)], levels=levels())
        return reply(v, tools=(ta_call(1),))

    def news(call: Any) -> Any:
        v = analyst("news", security_id=_sid(call), key_points=[point(1)])
        return reply(v, tools=calls("mcp__news__get_news", 1))

    def macro_(call: Any) -> Any:
        return reply(macro(), tools=calls("mcp__macro__get_series", 1))

    def mf(call: Any) -> Any:
        return reply(fund(security_id=_sid(call)), tools=calls("mcp__engine__mf_analyse", 1))

    def side(name: str) -> Any:
        def respond(call: Any) -> Any:
            b = json.loads(call.prompt)
            extra = {"strongest_unrebutted": "A fair worry"} if b["final_round"] else {}
            if b["summary"]:
                extra = {}
            return reply(
                turn(name, b["round"], security_id=_sid(call), summary=b["summary"], **extra)
            )

        return respond

    def lens_(name: str) -> Any:
        return lambda call: reply(lens(name, security_id=_sid(call)))

    def risk_(call: Any) -> Any:
        return reply(risk(security_id=_sid(call)), tools=calls("mcp__engine__risk_metrics", 1))

    def pm(call: Any) -> Any:
        return reply(
            verdict(
                security_id=_sid(call),
                verdict="BUY",
                suggested_weight_pct=3,
                bull_case="Quality compounder",
                bear_case="Valuation risk",
            )
        )

    by: dict[str, Any] = {
        "fundamental": fa, "technical": ta, "news": news, "macro": macro_, "mf": mf,
        "bull": side("bull"), "bear": side("bear"), "risk": risk_, "pm": pm,
        **{f"lens_{n}": lens_(n) for n in ("value", "growth", "contrarian", "valuation")},
    }  # fmt: skip
    by.update(replace)
    return script(**by)
