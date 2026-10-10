"""The rules-based screener (ST-6.8): a YAML rule file over a supplied list of securities.

A rule reads one metric of the registry (`metrics.py`) with a comparison: `<`, `<=`, `>`, `>=`,
`==`, `!=` or `between` (inclusive). A security matches when every rule that could be evaluated
passes; the values that passed come back with the match. A rule whose data is unavailable is
skipped and reported with the reason, never treated as passed or failed. A security with a skipped
rule is a partial match; a rule marked `required` rejects the security instead of being skipped;
a security whose rules were all skipped is not a match. Rules run cheapest bundle first, and the
first failed rule stops work on that security, so the expensive bundles run only for the
securities that survive. The universe is the explicit list the caller supplies (no investable
universe exists yet), and universe-relative metrics such as `rs_percentile` rank within it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from numbers import Real
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.errors import ConfigError
from nivesh_engine.metrics import (
    BUNDLE_COST,
    REGISTRY,
    Bundles,
    SecurityInputs,
    UniverseContext,
    evaluate,
)

Op = Literal["<", "<=", ">", ">=", "==", "!=", "between"]
Value = Decimal | str | tuple[Decimal, Decimal]


def _number(v: Any) -> Any:
    """YAML floats become exact decimals through their text; a flag is not a number."""
    if isinstance(v, bool):
        raise ValueError("a flag is not a number")
    if isinstance(v, Real) and not isinstance(v, int | Decimal):  # a YAML fraction
        return Decimal(str(v))
    if isinstance(v, list | tuple):
        return tuple(_number(x) for x in v)
    return v


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    metric: str
    op: Op
    value: Value
    required: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def _plain(cls, v: Any) -> Any:
        return _number(v)

    @model_validator(mode="after")
    def _consistent(self) -> "Rule":
        known = REGISTRY.get(self.metric)
        if known is None:
            raise ValueError(f"unknown metric {self.metric!r}")
        v = self.value
        if known.kind == "string":
            if self.op not in ("==", "!=") or not isinstance(v, str):
                raise ValueError(f"{self.metric} is text: use == or != with a text value")
        elif self.op == "between":
            if not (isinstance(v, tuple) and len(v) == 2 and v[0] <= v[1]):
                raise ValueError("between needs two numbers, lowest first")
        elif not isinstance(v, Decimal):
            raise ValueError(f"{self.metric} is numeric: {self.op} needs one number")
        return self


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    rules: tuple[Rule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "RuleSet":
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("rule ids must be unique")
        return self


def parse_rules(text: str, *, max_rules: int = 50) -> RuleSet:
    """Validate a YAML rule file; a failure is a ConfigError naming each offending field."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"rule file: invalid YAML ({type(e).__name__})") from e
    if not isinstance(data, dict):
        raise ConfigError("rule file: top level must be a mapping with a `rules` list")
    try:
        parsed = RuleSet.model_validate(data)
    except ValidationError as e:
        lines = [f"  {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError("rule file: invalid\n" + "\n".join(lines)) from None
    if len(parsed.rules) > max_rules:
        raise ConfigError(f"rule file has {len(parsed.rules)} rules; at most {max_rules} allowed")
    return parsed


def bundles_needed(rules: RuleSet) -> frozenset[str]:
    """The bundles (`ta`, `fa`, ...) the rule set reads."""
    return frozenset(REGISTRY[r.metric].bundle for r in rules.rules)


@dataclass(frozen=True)
class RuleValue:
    rule_id: str
    metric: str
    value: Decimal | str


@dataclass(frozen=True)
class Match:
    security_id: int
    symbol: str
    values: tuple[RuleValue, ...]  # the rules that passed, in rule-file order
    skipped_rules: tuple[str, ...]  # rules that could not be evaluated for this security
    partial: bool


@dataclass(frozen=True)
class SkippedRule:
    """A rule that was unavailable on every security it was evaluated on."""

    rule_id: str
    metric: str
    reason: str
    unavailable: int
    evaluated: int


@dataclass(frozen=True)
class Rejection:
    """A security rejected because a `required` rule had no data."""

    security_id: int
    rule_id: str
    reason: str


@dataclass(frozen=True)
class ScreenResult:
    matches: tuple[Match, ...]
    skipped: tuple[SkippedRule, ...]
    rejected: tuple[Rejection, ...]
    evaluated: int
    universe_basis: str


def _passes(value: Decimal | str, rule: Rule) -> bool:
    bound = rule.value
    if isinstance(value, str) or isinstance(bound, str):
        return (value == bound) if rule.op == "==" else (value != bound)
    if rule.op == "between":
        lo, hi = bound  # type: ignore[misc]
        return bool(lo <= value <= hi)
    if not isinstance(bound, Decimal):  # the rule validator rules this out
        return False
    return {
        "<": value < bound, "<=": value <= bound, ">": value > bound, ">=": value >= bound,
        "==": value == bound, "!=": value != bound,
    }[rule.op]  # fmt: skip


class _Tally:
    """Per rule: evaluations, unavailable evaluations and the first reason seen."""

    def __init__(self) -> None:
        self.evaluated = 0
        self.unavailable = 0
        self.reason = ""

    def miss(self, reason: str) -> None:
        self.unavailable += 1
        self.reason = self.reason or reason


def screen(
    universe: Mapping[int, SecurityInputs],
    rules: RuleSet,
    *,
    as_of: date,
    cfg: AnalysisSettings,
    basis: str | None = None,
) -> ScreenResult:
    """Matches with the values that passed, and the skipped and rejected rules, by security id.
    `basis` describes a named universe; without it the universe is an explicit id list."""
    if len(universe) > cfg.screen.max_universe:
        raise ValueError(
            f"universe of {len(universe)} exceeds analysis.screen.max_universe "
            f"({cfg.screen.max_universe})"
        )
    order = sorted(
        enumerate(rules.rules), key=lambda ir: (BUNDLE_COST[REGISTRY[ir[1].metric].bundle], ir[0])
    )
    plan: dict[str, set[str]] = {}
    for r in rules.rules:
        plan.setdefault(REGISTRY[r.metric].bundle, set()).add(r.metric)
    frozen_plan = {k: frozenset(v) for k, v in plan.items()}
    shared = UniverseContext(universe, as_of, cfg)
    tally = {r.id: _Tally() for r in rules.rules}
    matches: list[Match] = []
    rejected: list[Rejection] = []
    for sid in sorted(universe):
        bundles = Bundles(universe[sid], as_of, cfg, shared, frozen_plan)
        passed: dict[int, RuleValue] = {}
        skipped: list[int] = []
        dropped = False
        for index, rule in order:
            cell = evaluate(rule.metric, bundles)
            tally[rule.id].evaluated += 1
            if cell.value is None:
                why = cell.reason or "unavailable"
                tally[rule.id].miss(why)
                if rule.required:
                    rejected.append(Rejection(sid, rule.id, why))
                    dropped = True
                    break
                skipped.append(index)
                continue
            if not _passes(cell.value, rule):
                dropped = True
                break
            passed[index] = RuleValue(rule.id, rule.metric, cell.value)
        if dropped or not passed:
            continue
        skipped_ids = tuple(rules.rules[i].id for i in sorted(skipped))
        matches.append(
            Match(
                sid,
                universe[sid].symbol,
                tuple(passed[i] for i in sorted(passed)),
                skipped_ids,
                bool(skipped_ids),
            )  # fmt: skip
        )
    skips = tuple(
        SkippedRule(r.id, r.metric, t.reason, t.unavailable, t.evaluated)
        for r in rules.rules
        if not r.required and (t := tally[r.id]).evaluated and t.unavailable == t.evaluated
    )
    basis = basis or (
        f"explicit list of {len(universe)} securities supplied by the caller (no investable "
        "universe is defined yet); universe-relative metrics such as rs_percentile are ranked "
        "within it"
    )
    return ScreenResult(tuple(matches), skips, tuple(rejected), len(universe), basis)
