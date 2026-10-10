import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.config import Settings, load_settings
from nivesh_core.errors import ConfigError

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
GROUPS = [
    "ta", "levels", "regime", "setups", "fa", "valuation", "flags", "xray", "risk", "screen",
    "scoring",
]  # fmt: skip


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return load_settings(p)


def bad(tmp_path: Path, text: str, needle: str | None = None) -> None:
    with pytest.raises(ConfigError, match=needle):
        load(tmp_path, text)


def test_defaults_validate_without_an_analysis_block(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\n")
    assert s.analysis == AnalysisSettings()
    assert s.analysis.ta.min_bars_long == 200 and s.analysis.flags.pledge_pct == D(20)


def test_example_config_analysis_block_loads_and_equals_defaults() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.analysis == AnalysisSettings()


@pytest.mark.parametrize("group", GROUPS)
def test_unknown_key_is_rejected_in_every_group(tmp_path: Path, group: str) -> None:
    bad(tmp_path, f"analysis: {{{group}: {{no_such_field: 1}}}}\n", group)


def test_unknown_top_level_analysis_key_is_rejected(tmp_path: Path) -> None:
    bad(tmp_path, "analysis: {surprise: 1}\n", "analysis")


def test_scoring_weights_must_sum_to_100_per_horizon(tmp_path: Path) -> None:
    pos = "positional: {quality: 15, value: 10, growth: 20, momentum: 45, risk: 9}"
    bad(tmp_path, f"analysis: {{scoring: {{weights: {{{pos}}}}}}}\n", "sum to 100")
    lt = "long_term: {quality: 30, value: 25, growth: 25, momentum: 10, risk: 11}"
    bad(tmp_path, f"analysis: {{scoring: {{weights: {{{lt}}}}}}}\n", "sum to 100")


def test_default_weights_match_pid_15_5() -> None:
    w = AnalysisSettings().scoring.weights
    names = ("quality", "value", "growth", "momentum", "risk")
    assert [getattr(w.long_term, f) for f in names] == [D(30), D(25), D(25), D(10), D(10)]
    assert [getattr(w.positional, f) for f in names] == [D(15), D(10), D(20), D(45), D(10)]


def test_weights_version_must_be_a_nonempty_label(tmp_path: Path) -> None:
    bad(tmp_path, "analysis: {scoring: {weights_version: '  '}}\n", "weights_version")
    ok = load(tmp_path, "analysis: {scoring: {weights_version: my-label}}\n")
    assert ok.analysis.scoring.weights_version == "my-label"


def test_weights_digest_changes_when_any_weight_changes_and_is_stable_otherwise(
    tmp_path: Path,
) -> None:
    base = AnalysisSettings().scoring
    assert base.weights_digest() == AnalysisSettings().scoring.weights_digest()
    other = load(
        tmp_path,
        "analysis: {scoring: {weights: {long_term: "
        "{quality: 31, value: 24, growth: 25, momentum: 10, risk: 10}}}}\n",
    ).analysis.scoring
    assert other.weights_digest() != base.weights_digest()
    relabel = load(tmp_path, "analysis: {scoring: {weights_version: zz}}\n").analysis.scoring
    assert relabel.weights_digest() == base.weights_digest()  # the digest is about the weights
    assert re.fullmatch(r"[0-9a-f]{16}", base.weights_digest())


def _floats(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [k for k, v in node.items() if isinstance(v, float)] + [
            x for v in node.values() for x in _floats(v)
        ]
    if isinstance(node, list):
        return [x for v in node for x in _floats(v)]
    return []


def test_numeric_fields_are_decimal_not_float(tmp_path: Path) -> None:
    for s in (AnalysisSettings(), load_settings(ROOT / "config" / "nivesh.yaml").analysis):
        assert _floats(s.model_dump()) == []
        assert isinstance(s.levels.cluster_tolerance_pct, Decimal)
        assert isinstance(s.setups.breakout_volume_mult, Decimal)
        assert isinstance(s.valuation.dcf.base.discount_pct, Decimal)
    parsed = load(tmp_path, "analysis: {levels: {cluster_tolerance_pct: 2.5}}\n").analysis
    assert parsed.levels.cluster_tolerance_pct == D("2.5")


@pytest.mark.parametrize(
    "block",
    [
        "ta: {sma_windows: [20, 0]}",
        "ta: {sma_windows: []}",
        "ta: {rsi_period: 0}",
        "ta: {macd_fast: 30}",
        "levels: {pivot_window: 0}",
        "setups: {base_lookback: -1}",
        "valuation: {history_years: [5, -10]}",
        "valuation: {min_obs: 0}",
        "risk: {adv_days: 0}",
        "risk: {drawdown_years: [0]}",
        "xray: {top_n: [0]}",
        "screen: {max_universe: 0}",
        "scoring: {min_peers_for_sector: 0}",
    ],
)
def test_non_positive_windows_and_periods_are_rejected(tmp_path: Path, block: str) -> None:
    bad(tmp_path, f"analysis: {{{block}}}\n")


@pytest.mark.parametrize("scenario", ["base", "bull", "bear"])
def test_discount_rate_must_exceed_terminal_growth_in_every_scenario(
    tmp_path: Path, scenario: str
) -> None:
    sc = "{growth_pct: 5, discount_pct: 4, terminal_growth_pct: 4}"
    bad(tmp_path, f"analysis: {{valuation: {{dcf: {{{scenario}: {sc}}}}}}}\n", "exceed")


def test_flag_severity_values_limited_to_hard_or_soft(tmp_path: Path) -> None:
    bad(tmp_path, "analysis: {flags: {severity: {cfo_to_pat: severe}}}\n", "severity")
    ok = load(tmp_path, "analysis: {flags: {severity: {cfo_to_pat: soft}}}\n")
    assert ok.analysis.flags.severity == {"cfo_to_pat": "soft"}
    defaults = AnalysisSettings().flags.severity
    assert set(defaults.values()) == {"hard", "soft"}
    assert defaults["cfo_to_pat"] == "hard" and defaults["auditor_change"] == "soft"


def test_benchmark_sector_index_and_peer_override_maps_parse(tmp_path: Path) -> None:
    s = load(
        tmp_path,
        'analysis: {ta: {benchmarks: {IN: "NIFTY 50", US: SPX}, sector_index: {Banks: BANKIDX}},'
        " valuation: {peer_overrides: {AAA: [BBB, CCC]}}}\n",
    ).analysis
    assert s.ta.benchmarks == {"IN": "NIFTY 50", "US": "SPX"}
    assert s.ta.sector_index == {"Banks": "BANKIDX"}
    assert s.valuation.peer_overrides == {"AAA": ["BBB", "CCC"]}
    assert AnalysisSettings().ta.benchmarks == {}


def test_market_cap_thresholds_must_be_descending(tmp_path: Path) -> None:
    ok = load(tmp_path, "analysis: {xray: {market_cap: {IN: {large_min: 100, mid_min: 20}}}}\n")
    assert ok.analysis.xray.market_cap["IN"].large_min == D(100)
    bad(tmp_path, "analysis: {xray: {market_cap: {IN: {large_min: 20, mid_min: 100}}}}\n", "desc")
    bad(tmp_path, "analysis: {xray: {market_cap: {IN: {large_min: 20, mid_min: 20}}}}\n", "desc")


def test_band_minimums_must_descend_and_regime_breadth_must_be_ordered(tmp_path: Path) -> None:
    bad(tmp_path, "analysis: {scoring: {top_band_min: 50}}\n", "descend")
    bad(tmp_path, "analysis: {regime: {risk_on_breadth_pct: 30}}\n", "exceed")
    bad(tmp_path, "analysis: {setups: {precedence: [breakout, none]}}\n", "precedence")


def _keys(node: Any) -> list[str]:
    if isinstance(node, BaseModel):
        return _keys(node.model_dump())
    if isinstance(node, dict):
        return [str(k) for k in node] + [x for v in node.values() for x in _keys(v)]
    if isinstance(node, list):
        return [x for v in node for x in _keys(v)]
    return []


def test_secret_shaped_keys_are_absent_from_the_analysis_block() -> None:
    pattern = re.compile(r"(_key|_token|_secret|password|credential)$", re.IGNORECASE)
    assert [k for k in _keys(AnalysisSettings()) if pattern.search(k)] == []
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    block = text[text.index("\nanalysis:") :]
    assert not re.search(r"(_key|_token|_secret|password)\s*:", block, re.IGNORECASE)


def test_other_settings_blocks_still_load_unchanged(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\nmf: {nav_gap_days: 7}\n")
    assert s.mf.nav_gap_days == 7 and s.mf.thresholds.overlap_pct == D(60)
    assert s.data_dir == "x" and s.analysis == AnalysisSettings()
