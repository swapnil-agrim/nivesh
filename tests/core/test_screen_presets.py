"""The four YAML presets of ST-9.2: they load, they run through the screener on a seeded universe,
and none lets trailing return stand alone (BR-12)."""

import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.pii_scan import scan_text
from nivesh_engine.metrics import REGISTRY, SecurityInputs, inputs_needed
from nivesh_engine.screen import RuleSet, parse_rules, screen
from tests.analysis_fx import (
    UNIVERSE_ASOF,
    annual_rows,
    bars_from_closes,
    legs_closes,
    seed_universe,
)

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
SCREENS = ROOT / "config" / "screens"
NAMES = ["lt-quality-growth", "lt-quality-value", "pos-breakout", "pos-pullback-uptrend"]
CFG = AnalysisSettings()
FUNDAMENTAL_BUNDLES = {"fa", "valuation", "estimates"}
TECHNICAL_PREFIXES = ("roc_", "rs_", "price_vs_")
TECHNICAL_NAMES = {"base_breakout", "pullback_to_50dma", "setup_type"}


def load(name: str) -> RuleSet:
    return parse_rules((SCREENS / f"{name}.yaml").read_text(), max_rules=CFG.screen.max_rules)


def lint(rules: RuleSet) -> list[str]:
    """Rules on trailing return or price-relative metrics that no required fundamentals rule backs.
    The bundle of each metric comes from the registry; only the technical prefixes are named."""
    technical = [
        r.id for r in rules.rules
        if r.metric.startswith(TECHNICAL_PREFIXES) or r.metric in TECHNICAL_NAMES
    ]  # fmt: skip
    backed = any(r.required and REGISTRY[r.metric].bundle in FUNDAMENTAL_BUNDLES
                 for r in rules.rules)  # fmt: skip
    return [] if backed else technical


def test_preset_names_are_exactly_the_four() -> None:
    assert sorted(p.stem for p in SCREENS.glob("*.yaml")) == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_each_preset_loads_as_a_ruleset_within_max_rules(name: str) -> None:
    rules = load(name)
    assert rules.name == name and 1 <= len(rules.rules) <= CFG.screen.max_rules


@pytest.mark.parametrize("name", NAMES)
def test_presets_use_only_registered_metrics(name: str) -> None:
    assert {r.metric for r in load(name).rules} <= REGISTRY.keys()


@pytest.mark.parametrize("name", NAMES)
def test_preset_horizon_label_long_term_or_positional(name: str) -> None:
    assert name.startswith(("lt-", "pos-"))


def test_lint_no_preset_uses_trailing_return_as_standalone_criterion() -> None:
    for name in NAMES:
        assert lint(load(name)) == [], name
    assert any(r.metric == "rs_percentile" for r in load("pos-breakout").rules)


def test_lint_fails_on_a_synthetic_bad_preset() -> None:
    bad = parse_rules("name: bad\nrules:\n  - {id: mom, metric: roc_6m, op: '>', value: 10}\n")
    assert lint(bad) == ["mom"]
    soft = parse_rules(
        "name: soft\nrules:\n  - {id: mom, metric: roc_6m, op: '>', value: 10}\n"
        "  - {id: q, metric: roce, op: '>', value: 10}\n"  # not required: does not back it
    )
    assert lint(soft) == ["mom"]
    ok = parse_rules(
        "name: ok\nrules:\n  - {id: mom, metric: roc_6m, op: '>', value: 10}\n"
        "  - {id: q, metric: roce, op: '>', value: 10, required: true}\n"
    )
    assert lint(ok) == []


def test_presets_have_no_credential_looking_literals() -> None:
    for p in SCREENS.glob("*.yaml"):
        text = p.read_text()
        assert scan_text(text) == [], p.name
        assert not re.search(r"secret|token|password|credential|api_key|private", text, re.I)
        assert not re.search(r"[A-Za-z0-9]{20,}", text)


# ---- they run through the screener ---------------------------------------------------------------
def universe() -> dict[int, SecurityInputs]:
    """Thirty synthetic securities; ids 3 and 7 are fast growers; 5 and 9 break out on volume."""
    base = seed_universe(30)
    grow = {
        "revenue": [1000, 1150, 1320, 1520, 1750, 2010],
        "net_income": [100, 120, 145, 175, 210, 250],
        "eps": [3, 4, 5, 7, 8, 10],
        "operating_income": [150, 175, 205, 240, 280, 330],
        "total_equity": [400, 420, 440, 460, 480, 500],
        "total_debt": [200] * 6,
        "cash": [100] * 6,
        "depreciation_amortization": [40] * 6,
        "current_assets": [300] * 6,
        "current_liabilities": [200] * 6,
        "total_assets": [1000, 1030, 1060, 1090, 1120, 1150],
        "cfo": [110, 130, 150, 175, 200, 230],
        "capex": [40] * 6,
        "gross_profit": [360, 400, 450, 500, 560, 620],
    }
    rows = tuple(annual_rows(grow, last_fy=2023))
    closes = legs_closes(100, [(200, "1"), (30, "-1"), (33, "1"), (1, "5")])
    n = len(closes)
    up = bars_from_closes(closes, spread=14, start=299 - (n - 1), volumes={n - 1: 3000})
    for i in (3, 7):
        v = base[i].valuation
        assert v is not None
        base[i] = replace(base[i], rows=rows, valuation=replace(v, rows=rows))
    for i in (5, 9):
        base[i] = replace(base[i], bars=up)
    return base


@pytest.fixture(scope="module")
def run() -> dict[str, tuple[RuleSet, list[int]]]:
    uni = universe()
    out = {}
    for name in NAMES:
        rules = load(name)
        # only the inputs the preset needs are read in production; here all are present
        assert inputs_needed(r.metric for r in rules.rules)
        found = screen(uni, rules, as_of=UNIVERSE_ASOF, cfg=CFG)
        out[name] = (rules, [m.security_id for m in found.matches])
        assert found.evaluated == 30
    return out


@pytest.mark.parametrize("name", NAMES)
def test_each_preset_returns_matches_on_the_seeded_universe(
    name: str, run: dict[str, tuple[RuleSet, list[int]]]
) -> None:
    matched = run[name][1]
    assert 1 <= len(matched) < 30, (name, matched)


def test_matches_carry_the_values_that_passed() -> None:
    found = screen(universe(), load("pos-pullback-uptrend"), as_of=UNIVERSE_ASOF, cfg=CFG)
    assert [m.security_id for m in found.matches] == [18, 26]
    for m in found.matches:
        assert [v.rule_id for v in m.values] == ["sound_business", "above_200dma", "pullback_setup"]
        assert next(v for v in m.values if v.rule_id == "pullback_setup").value == D(1)


def test_growth_preset_picks_the_growers_and_breakout_picks_the_breakouts(
    run: dict[str, tuple[RuleSet, list[int]]],
) -> None:
    assert set(run["lt-quality-growth"][1]) == {3, 7}
    assert set(run["pos-breakout"][1]) == {5, 9}
