import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from nivesh_core.config import IndiaTax, MfTax, Settings, load_settings
from nivesh_core.errors import ConfigError
from nivesh_core.profile import load_profile
from nivesh_core.review_config import ReviewSettings

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return load_settings(p)


def bad(tmp_path: Path, text: str, needle: str | None = None) -> None:
    with pytest.raises(ConfigError, match=needle):
        load(tmp_path, text)


def test_defaults_validate_without_review_or_india_blocks(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\n")
    assert s.review == ReviewSettings()
    r = s.review
    assert (r.default_review_days, r.band_pp, r.turnover_limit_pct) == (90, D(5), D(30))
    assert (r.valuation_percentile_min, r.valuation_metric, r.valuation_history_years) == (
        D(95),
        "pe",
        5,
    )
    assert (r.below_sma200_sessions, r.deterioration_quarters) == (60, 2)
    assert s.tax.india == IndiaTax()


def test_example_review_block_loads_and_equals_defaults() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.review == ReviewSettings()
    assert s.tax.india == IndiaTax()
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    assert re.search(r"^review:", text, re.MULTILINE)
    assert "# india:" in text


def test_unknown_key_rejected_in_review_and_tax_india(tmp_path: Path) -> None:
    bad(tmp_path, "review: {nope: 1}\n", "nope")
    bad(tmp_path, "tax: {india: {nope: 1}}\n", "nope")


@pytest.mark.parametrize(
    "block",
    [
        "band_pp: 0",
        "band_pp: -1",
        "turnover_limit_pct: 0",
        "turnover_limit_pct: 101",
        "valuation_percentile_min: -1",
        "valuation_percentile_min: 101",
        "default_review_days: 0",
        "below_sma200_sessions: 0",
        "deterioration_quarters: 0",
        "valuation_history_years: 0",
        "valuation_metric: ' '",
    ],
)
def test_review_numbers_are_decimal_and_positive(tmp_path: Path, block: str) -> None:
    bad(tmp_path, f"review: {{{block}}}\n", "review")


def test_review_numbers_parse_as_exact_decimals(tmp_path: Path) -> None:
    r = load(tmp_path, "review: {band_pp: 2.5, turnover_limit_pct: 100}\n").review
    assert r.band_pp == D("2.5") and isinstance(r.band_pp, Decimal)
    assert r.turnover_limit_pct == D(100)


def test_india_tax_all_unset_by_default() -> None:
    t = IndiaTax()
    assert (t.long_term_days, t.short_rate_pct, t.long_rate_pct, t.ltcg_exemption_inr) == (
        None,
        None,
        None,
        None,
    )


def test_india_tax_rates_bounded_0_100_and_days_positive_and_exemption_non_negative(
    tmp_path: Path,
) -> None:
    ok = load(
        tmp_path,
        "tax: {india: {long_term_days: 366, short_rate_pct: 20, long_rate_pct: 12.5, "
        "ltcg_exemption_inr: 125000}}\n",
    ).tax.india
    assert ok.long_term_days == 366 and ok.long_rate_pct == D("12.5")
    assert ok.ltcg_exemption_inr == D(125000)
    for block in (
        "long_term_days: 0",
        "short_rate_pct: -1",
        "short_rate_pct: 101",
        "long_rate_pct: 101",
        "ltcg_exemption_inr: -1",
    ):
        bad(tmp_path, f"tax: {{india: {{{block}}}}}\n", "india")


def test_us_long_term_days_and_mf_tax_unchanged(tmp_path: Path) -> None:
    s = load(tmp_path, "tax: {us_long_term_days: 365}\nmf: {tax: {long_term_days: 400}}\n")
    assert s.tax.us_long_term_days == 365 and s.tax.india == IndiaTax()
    assert s.mf.tax == MfTax(long_term_days=400)


def test_profile_tax_rates_still_loads_and_no_code_reads_it() -> None:
    p = load_profile(ROOT / "config" / "profile.yaml")
    assert p.tax_rates  # legacy field still parses
    hits = []
    for pkg in ("nivesh_core", "nivesh_engine", "nivesh_adapters", "nivesh_agents", "nivesh_cli"):
        for f in (ROOT / pkg).rglob("*.py"):
            if f.name == "profile.py":
                continue
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.Attribute) and node.attr == "tax_rates":
                    hits.append(str(f))
    assert hits == []


def _names(m: type[BaseModel]) -> list[str]:
    out: list[str] = []
    for n, f in m.model_fields.items():
        out.append(n)
        a = f.annotation
        if isinstance(a, type) and issubclass(a, BaseModel):
            out += _names(a)
    return out


def test_no_field_name_looks_like_a_credential() -> None:
    names = _names(ReviewSettings) + _names(IndiaTax)
    pattern = r"(_key|_token|_secret|password|credential)$"
    assert names and not [n for n in names if re.search(pattern, n)]
