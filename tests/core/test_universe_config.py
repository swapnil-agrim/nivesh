import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from nivesh_core import universe_config
from nivesh_core.config import Settings, load_settings
from nivesh_core.errors import ConfigError
from nivesh_core.universe_config import IdeasSettings, UniverseSettings

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return load_settings(p)


def bad(tmp_path: Path, text: str, needle: str | None = None) -> None:
    with pytest.raises(ConfigError, match=needle):
        load(tmp_path, text)


def test_defaults_validate_without_universe_or_ideas_blocks(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\n")
    assert s.universe == UniverseSettings() and s.ideas == IdeasSettings()


def test_example_blocks_load_and_equal_defaults() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.universe == UniverseSettings() and s.ideas == IdeasSettings()
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    assert re.search(r"^universe:", text, re.MULTILINE)
    assert re.search(r"^ideas:", text, re.MULTILINE)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    bad(tmp_path, "universe: {nope: 1}\n", "nope")
    bad(tmp_path, "ideas: {nope: 1}\n", "nope")
    bad(tmp_path, "universe: {liquidity: {IN: {nope: 1}}}\n", "nope")


def test_default_liquidity_floors_5_crore_and_20_usd_million() -> None:
    u = UniverseSettings()
    assert u.liquidity.IN.min_adv_crore == D(5)
    assert u.liquidity.US.min_adv_usd_m == D(20)
    assert (u.max_age_days, u.max_bar_age_days) == (35, 7)


def test_numbers_are_decimal_and_positive(tmp_path: Path) -> None:
    s = load(tmp_path, "universe: {liquidity: {IN: {min_adv_crore: 2.5}}}\n")
    assert s.universe.liquidity.IN.min_adv_crore == D("2.5")
    for block in (
        "universe: {liquidity: {IN: {min_adv_crore: 0}}}",
        "universe: {liquidity: {US: {min_adv_usd_m: -1}}}",
        "universe: {max_age_days: 0}",
        "universe: {max_bar_age_days: 0}",
        "ideas: {sector_cap: 0}",
        "ideas: {default_n: 0}",
        "ideas: {held: maybe}",
    ):
        bad(tmp_path, block + "\n")


def test_shortlist_size_above_max_rejected(tmp_path: Path) -> None:
    bad(tmp_path, "ideas: {shortlist_size: 13}\n", "shortlist_size")
    bad(tmp_path, "ideas: {shortlist_size: 9, shortlist_max: 8}\n", "shortlist_size")
    assert load(tmp_path, "ideas: {shortlist_size: 12}\n").ideas.shortlist_size == 12


def test_shortlist_defaults_8_cap_2_n_5_max_12() -> None:
    i = IdeasSettings()
    assert (i.shortlist_size, i.shortlist_max, i.sector_cap, i.default_n) == (8, 12, 2, 5)
    assert i.held == "label" and i.max_runs == 16


def test_default_indices_are_nifty500_sp500_nasdaq100_with_smallcap_and_russell_optional() -> None:
    idx = UniverseSettings().indices
    on = {k for k, v in idx.items() if v.enabled}
    assert on == {"NIFTY500", "SP500", "NASDAQ100"}
    assert {k for k in idx if k not in on} == {"NIFTYSMALLCAP250", "RUSSELL1000"}
    assert {k: v.market for k, v in idx.items()} == {
        "NIFTY500": "IN", "NIFTYSMALLCAP250": "IN", "SP500": "US", "NASDAQ100": "US",
        "RUSSELL1000": "US",
    }  # fmt: skip


def test_index_names_are_plain(tmp_path: Path) -> None:
    bad(tmp_path, "universe: {indices: {'bad name': {market: IN}}}\n")


def test_no_field_name_looks_like_a_credential() -> None:
    tree = ast.parse(Path(universe_config.__file__).read_text())
    names = [n.target.id for n in ast.walk(tree) if isinstance(n, ast.AnnAssign)
             and isinstance(n.target, ast.Name)]  # fmt: skip
    assert names
    cred = re.compile(r"secret|token|password|credential|api_key|private|_key$", re.I)
    assert not [n for n in names if cred.search(n)]
    for m in (UniverseSettings, IdeasSettings):
        assert issubclass(m, BaseModel)
