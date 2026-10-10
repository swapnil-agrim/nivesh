import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from nivesh_core.agents_config import AGENTS, AgentsSettings
from nivesh_core.config import Settings, load_settings
from nivesh_core.errors import ConfigError

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return load_settings(p)


def bad(tmp_path: Path, text: str, needle: str | None = None) -> None:
    with pytest.raises(ConfigError, match=needle):
        load(tmp_path, text)


def test_defaults_validate_without_an_agents_block(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\n")
    assert s.agents == AgentsSettings()
    assert s.agents.concurrency == 4 and s.agents.lenses_enabled is True
    assert s.agents.max_filing_sections == 2


def test_example_config_agents_block_loads_and_equals_defaults() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.agents == AgentsSettings()
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    assert re.search(r"^agents:", text, re.MULTILINE)


def test_unknown_key_is_rejected_in_every_group(tmp_path: Path) -> None:
    bad(tmp_path, "agents: {nope: 1}\n", "nope")
    bad(tmp_path, "agents: {models: {nope: x}}\n", "nope")


def test_concurrency_default_4_and_range_1_to_16(tmp_path: Path) -> None:
    assert load(tmp_path, "data_dir: x\n").agents.concurrency == 4
    for ok in (1, 16):
        assert load(tmp_path, f"agents: {{concurrency: {ok}}}\n").agents.concurrency == ok
    for no in (0, 17, -1):
        bad(tmp_path, f"agents: {{concurrency: {no}}}\n", "concurrency")


def test_debate_rounds_default_2_and_range_1_to_3(tmp_path: Path) -> None:
    assert load(tmp_path, "data_dir: x\n").agents.debate_rounds == 2
    for ok in (1, 3):
        assert load(tmp_path, f"agents: {{debate_rounds: {ok}}}\n").agents.debate_rounds == ok
    for no in (0, 4):
        bad(tmp_path, f"agents: {{debate_rounds: {no}}}\n", "debate_rounds")


def test_min_coverage_pct_default_70_and_must_be_in_0_100(tmp_path: Path) -> None:
    assert load(tmp_path, "data_dir: x\n").agents.min_coverage_pct == D(70)
    for no in (-1, 101):
        bad(tmp_path, f"agents: {{min_coverage_pct: {no}}}\n", "min_coverage_pct")


def test_tier_map_covers_every_agent_and_only_top_mid_small(tmp_path: Path) -> None:
    t = AgentsSettings().tiers
    assert set(t) == set(AGENTS) and set(t.values()) <= {"top", "mid", "small"}
    assert t["fundamental"] == "top" and t["technical"] == "mid" and t["pm"] == "top"
    assert t["bull"] == "top" and t["lens"] == "mid" and t["risk"] == "top"
    assert t["thesis_draft"] == "top" and t["holding_review"] == "top"
    bad(tmp_path, "agents: {tiers: {pm: huge}}\n", "tiers")
    bad(tmp_path, "agents: {tiers: {nobody: top}}\n", "tiers")


def test_lenses_limited_to_the_four_known_names(tmp_path: Path) -> None:
    assert AgentsSettings().lenses == ["value", "growth", "contrarian", "valuation"]
    assert load(tmp_path, "agents: {lenses: [value]}\n").agents.lenses == ["value"]
    bad(tmp_path, "agents: {lenses: [astrology]}\n", "lenses")


def test_prompt_pins_must_be_positive_integers(tmp_path: Path) -> None:
    assert load(tmp_path, "agents: {prompt_pins: {pm: 2}}\n").agents.prompt_pins == {"pm": 2}
    bad(tmp_path, "agents: {prompt_pins: {pm: 0}}\n", "prompt_pins")
    bad(tmp_path, "agents: {prompt_pins: {nobody: 1}}\n", "prompt_pins")


def test_starter_weight_must_be_positive_and_below_100(tmp_path: Path) -> None:
    assert AgentsSettings().starter_weight_pct > 0
    for no in (0, -1, 100):
        bad(tmp_path, f"agents: {{starter_weight_pct: {no}}}\n", "starter_weight_pct")


def test_max_days_to_trade_default_none(tmp_path: Path) -> None:
    assert AgentsSettings().max_days_to_trade is None
    assert load(tmp_path, "agents: {max_days_to_trade: 5}\n").agents.max_days_to_trade == D(5)
    bad(tmp_path, "agents: {max_days_to_trade: 0}\n", "max_days_to_trade")


def test_numeric_fields_are_decimal_not_float() -> None:
    s = AgentsSettings(max_budget_usd=D("1.5"))
    assert isinstance(s.min_coverage_pct, Decimal) and isinstance(s.starter_weight_pct, Decimal)
    assert isinstance(s.max_budget_usd, Decimal)


def test_secret_shaped_keys_are_absent_from_the_agents_block() -> None:
    names: list[str] = []

    def walk(m: type[BaseModel]) -> None:
        for n, f in m.model_fields.items():
            names.append(n)
            a = f.annotation
            if isinstance(a, type) and issubclass(a, BaseModel):
                walk(a)

    walk(AgentsSettings)
    assert names and not [n for n in names if re.search(r"(_key|_token|_secret|password)$", n)]


def test_other_settings_blocks_still_load_unchanged(tmp_path: Path) -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.analysis.scoring.weights_version == "pid-15.5-v1"
    assert s.mf.nav_gap_days == 5 and s.usd_inr == 90.0
