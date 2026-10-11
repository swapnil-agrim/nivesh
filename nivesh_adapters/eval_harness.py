"""Golden-set evaluation harness (ST-12.6, ST-12.7): model-free cases with expected properties.

Each case names a `kind`, gives inputs and the properties it expects. A checker runs the real
engine for that kind (scoring, scorecard, calibration, look-ahead guard, committee rules,
citation check, ledger triggers) and the harness compares the properties it returns. No case
reaches a model or the network. A run is gated against a committed baseline: it blocks a release
when the pass rate falls by more than the allowed percentage points or any safety case fails.
A case marked `historical` is first checked by the look-ahead guard and is refused, as a
recorded failure, when the guard refuses.
"""

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from nivesh_core.analysis_config import AnalysisSettings, ScoringSettings
from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.errors import NiveshError
from nivesh_engine import dmath
from nivesh_engine.calibration import CalCall, calibrate
from nivesh_engine.citations import Evidence, validate
from nivesh_engine.committee_rules import RiskFacts, risk_veto, verdict_ceiling
from nivesh_engine.ledger_score import Series, score_call
from nivesh_engine.lookahead import assert_out_of_sample
from nivesh_engine.scorecard import ScoredCall, build_scorecard
from nivesh_engine.scoring import SecurityMeta, score_universe
from nivesh_mcp.base import is_write_name

D = Decimal
MAX_DROP_PP = D(5)


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    safety: bool
    historical: bool
    given: Mapping[str, Any]
    expect: Mapping[str, Any]


@dataclass(frozen=True)
class Outcome:
    id: str
    passed: bool
    reason: str
    safety: bool = False


@dataclass(frozen=True)
class Report:
    pass_rate_pct: Decimal
    outcomes: tuple[Outcome, ...]

    @property
    def failed(self) -> tuple[Outcome, ...]:
        return tuple(o for o in self.outcomes if not o.passed)


@dataclass(frozen=True)
class GateResult:
    blocked: bool
    reason: str


# ---- helpers -----------------------------------------------------------------------------------
def _day(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _dec(v: Any) -> Decimal:
    return D(str(v))


def _series(pairs: Sequence[Sequence[Any]]) -> Series:
    return Series.of([(_day(d), _dec(c)) for d, c in pairs])


def _norm(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "none"
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _no_floats(obj: Any, where: str) -> None:
    if isinstance(obj, float):
        raise NiveshError(f"{where}: decimals must be quoted strings, not floats")
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            _no_floats(v, f"{where}.{k}")
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            _no_floats(v, f"{where}[{i}]")


# ---- checkers: each returns the properties of what the real engine produced ----------------------
def _check_score_call(g: Mapping[str, Any]) -> dict[str, Any]:
    res = score_call(
        str(g["verdict"]), _day(g["as_of"]),
        None if g.get("price_date") is None else _day(g["price_date"]),
        _series(g["security"]), _series(g["benchmark"]), int(g["horizon_weeks"]),
        _dec(g.get("cost_pct", "0")), _day(g["today"]), int(g.get("stale_days", 7)),
    )  # fmt: skip
    if res is None:
        return {"status": "pending"}
    return {
        "status": res.status, "ret": res.ret, "benchmark_ret": res.benchmark_ret,
        "excess": res.excess, "call_excess": res.call_excess, "hit": res.hit,
        "end_date": res.end_date,
    }  # fmt: skip


def _calls(spec: Sequence[Mapping[str, Any]]) -> list[ScoredCall]:
    """Expand case items into scored calls. `count` repeats an item as distinct calls and
    `horizons` gives one call a row at each of those horizons."""
    out: list[ScoredCall] = []
    call_id = 0
    for item in spec:
        for _ in range(int(item.get("count", 1))):
            call_id += 1
            excess = item.get("excess")
            ce = None if excess is None else _dec(excess)
            for horizon in item.get("horizons") or [item.get("horizon", 13)]:
                out.append(
                    ScoredCall(
                        call_id,
                        str(item.get("verdict", "BUY")),
                        int(horizon),
                        str(item.get("market", "IN")),
                        str(item.get("conviction", "high")),
                        str(item.get("command", "ideas")),
                        bool(item.get("acted", False)),
                        str(item.get("status", "scored")),
                        ce,
                        None if ce is None else ce > 0,
                    )  # fmt: skip
                )
    return out


def _check_scorecard_bucket(g: Mapping[str, Any]) -> dict[str, Any]:
    card = build_scorecard(
        _calls(g["calls"]), int(g.get("min_bucket", 20)), int(g.get("primary_horizon_weeks", 13))
    )
    hits = [b for b in card.buckets[str(g["dimension"])] if b.key == str(g["key"])]
    if not hits:
        return {"found": False}
    b = hits[0]
    return {
        "found": True, "calls": b.calls, "scored": b.scored, "hit_pct": b.hit_pct,
        "mean_excess": b.mean_excess, "median_excess": b.median_excess, "worst": b.worst,
        "enough": b.enough,
    }  # fmt: skip


def _check_calibration_verdict(g: Mapping[str, Any]) -> dict[str, Any]:
    calls: list[CalCall] = []
    for level, (hits, misses) in g["groups"].items():
        for hit in [True] * int(hits) + [False] * int(misses):
            calls.append(CalCall(len(calls) + 1, str(level), hit, D(2) if hit else D(-1)))
    cal = calibrate(
        calls, int(g.get("calibration_min", 40)), int(g.get("resamples", 500)),
        int(g.get("resample_seed", 20260101)),
    )  # fmt: skip
    return {"enough": cal.enough, "n": cal.n, "verdict": cal.verdict}


def _check_lookahead(g: Mapping[str, Any]) -> dict[str, Any]:
    cutoffs = {str(k): _day(v) for k, v in (g.get("cutoffs") or {}).items()}
    try:
        assert_out_of_sample(_day(g["eval_start"]), str(g["model_id"]), cutoffs)
    except NiveshError:
        return {"outcome": "refused"}
    return {"outcome": "allowed"}


def _check_committee_cap(g: Mapping[str, Any]) -> dict[str, Any]:
    res = verdict_ceiling(
        str(g["proposed"]), veto=bool(g.get("veto", False)),
        card_band=str(g.get("card_band", "top")),
        card_cap=g.get("card_cap"), hard_flag=bool(g.get("hard_flag", False)),
        coverage=_dec(g.get("coverage", "100")), min_coverage=_dec(g.get("min_coverage", "70")),
    )  # fmt: skip
    return {"verdict": res.verdict, "vetoed_by_risk": res.vetoed_by_risk}


def _check_committee_veto(g: Mapping[str, Any]) -> dict[str, Any]:
    days = g.get("days_to_trade")
    facts = RiskFacts(
        excluded=bool(g.get("excluded", False)), hard_flag=bool(g.get("hard_flag", False)),
        position_over_limit=bool(g.get("position_over_limit", False)),
        sector_over_limit=bool(g.get("sector_over_limit", False)),
        days_to_trade=None if days is None else _dec(days), max_position_pct=D(10),
        max_sector_pct=D(30), sector_headroom_pct=None, tested_weight_pct=D(2),
    )  # fmt: skip
    cap = g.get("max_days_to_trade")
    res = risk_veto(facts, max_days_to_trade=None if cap is None else _dec(cap))
    return {"veto": res.veto, "reasons": ",".join(res.reasons)}


def _check_momentum_only(g: Mapping[str, Any]) -> dict[str, Any]:
    loose = AnalysisSettings(scoring=ScoringSettings(min_input_pct=D(1), min_peers_for_sector=4))
    raw: dict[int, Mapping[str, Decimal | None]] = {
        1: {"price_vs_sma200_pct": _dec(g["momentum"])},
        2: {"price_vs_sma200_pct": _dec(g.get("peer_momentum", "1"))},
    }
    meta = {1: SecurityMeta("US", "Tech"), 2: SecurityMeta("US", "Tech")}
    card = score_universe(raw, meta, {}, horizon="long_term", cfg=loose)[1]
    return {
        "band": card.band,
        "top_band": card.band == "top",
        "momentum_credit": card.momentum_credit,
    }


def _check_citation(g: Mapping[str, Any]) -> dict[str, Any]:
    ev = Evidence()
    for n in g.get("evidence", []):
        ev.add_number(_dec(n))
    for d in g.get("evidence_days", []):
        ev.add_day(_day(d))
    result = validate([("b1", "all", str(g["text"]))], ev, int(g.get("max_digits", 6)))
    return {"clean": result.clean, "unmatched": len(result.unmatched)}


def _check_ledger_append_only(g: Mapping[str, Any]) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")
    migrate.apply(conn, MIGRATIONS / "sqlite")
    table = str(g["table"])
    conn.execute(str(g["insert"]))
    stmt = f"UPDATE {table} SET id = id" if g["op"] == "update" else f"DELETE FROM {table}"  # noqa: S608
    try:
        conn.execute(stmt)
    except sqlite3.DatabaseError:
        return {"refused": True}
    finally:
        conn.close()
    return {"refused": False}


def _check_write_name(g: Mapping[str, Any]) -> dict[str, Any]:
    return {"write": is_write_name(str(g["name"]))}


CHECKERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "score_call": _check_score_call,
    "scorecard_bucket": _check_scorecard_bucket,
    "calibration_verdict": _check_calibration_verdict,
    "lookahead": _check_lookahead,
    "committee_cap": _check_committee_cap,
    "committee_veto": _check_committee_veto,
    "momentum_only": _check_momentum_only,
    "citation_check": _check_citation,
    "ledger_append_only": _check_ledger_append_only,
    "write_name": _check_write_name,
}


# ---- loading, running, gating ---------------------------------------------------------------
def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise NiveshError(f"{path.name}: expected a top-level 'cases' list")
    out: list[Case] = []
    seen: set[str] = set()
    for i, item in enumerate(raw["cases"]):
        cid = str(item.get("id", f"#{i}"))
        if cid in seen:
            raise NiveshError(f"duplicate case id {cid!r}")
        seen.add(cid)
        kind = str(item.get("kind"))
        if kind not in CHECKERS:
            raise NiveshError(f"case {cid!r}: unknown kind {kind!r}")
        if not item.get("expect"):
            raise NiveshError(f"case {cid!r}: no expected properties")
        _no_floats(item, cid)
        out.append(
            Case(
                cid,
                kind,
                bool(item.get("safety", False)),
                bool(item.get("historical", False)),
                item.get("given") or {},
                item["expect"],
            )  # fmt: skip
        )
    return out


def run_case(case: Case, cutoffs: Mapping[str, date] | None = None) -> Outcome:
    """Run one case and say, with a reason, whether its expected properties held."""
    if case.historical:
        try:
            assert_out_of_sample(
                _day(case.given["eval_start"]), str(case.given["model_id"]), cutoffs or {}
            )
        except NiveshError as e:
            return Outcome(case.id, False, f"refused by the look-ahead guard: {e}", case.safety)
    try:
        actual = CHECKERS[case.kind](case.given)
    except Exception as e:  # a broken case is a failed case, never a crash of the run
        return Outcome(case.id, False, f"checker error: {type(e).__name__}: {e}", case.safety)
    diffs = [
        f"{k}: expected {_norm(v)}, got {_norm(actual.get(k))}"
        for k, v in case.expect.items()
        if _norm(actual.get(k)) != _norm(v)
    ]
    if diffs:
        return Outcome(case.id, False, "; ".join(diffs), case.safety)
    return Outcome(case.id, True, "ok", case.safety)


def run_all(cases: Sequence[Case], cutoffs: Mapping[str, date] | None = None) -> Report:
    outcomes = tuple(run_case(c, cutoffs) for c in cases)
    passed = sum(1 for o in outcomes if o.passed)
    rate = D(100) if not outcomes else dmath.quantize(D(passed) * 100 / len(outcomes), D("0.01"))
    return Report(rate, outcomes)


def load_baseline(path: Path) -> Decimal | None:
    """The committed pass rate in percent, or None when there is no baseline file."""
    if not path.exists():
        return None
    return _dec(json.loads(path.read_text())["pass_rate_pct"])


def gate(
    report: Report, baseline: Decimal | None, max_drop_pp: Decimal = MAX_DROP_PP
) -> GateResult:
    """Block on any failed safety case, or when the pass rate fell by more than `max_drop_pp`
    percentage points below the baseline. With no baseline it only reports."""
    unsafe = [o.id for o in report.outcomes if o.safety and not o.passed]
    if unsafe:
        return GateResult(True, f"safety case failed: {', '.join(unsafe)}")
    if baseline is None:
        return GateResult(False, "report only: no baseline")
    drop = baseline - report.pass_rate_pct
    if drop > max_drop_pp:
        return GateResult(
            True, f"pass rate {report.pass_rate_pct}% is {drop} pp below the baseline {baseline}%"
        )
    return GateResult(False, f"pass rate {report.pass_rate_pct}% against baseline {baseline}%")
