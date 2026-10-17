from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.config import Settings, load_settings
from nivesh_core.errors import ConfigError
from nivesh_core.fundamentals_config import (
    CompareSettings,
    FundamentalsSettings,
    PlanSettings,
    ProviderSpec,
    ReservePct,
)
from nivesh_core.redact import is_sensitive_key
from nivesh_core.yamlio import validate

ROOT = Path(__file__).resolve().parents[2]
D = Decimal


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text("data_dir: x\n" + text)
    return load_settings(p)


def test_defaults_reproduce_todays_setup() -> None:
    f = FundamentalsSettings()
    assert [p.name for p in f.providers] == ["tapetide", "bse_xbrl", "upstox"]
    tape, bse, upstox = f.providers
    assert (tape.enabled, tape.role, tape.credential) == (True, "primary", "ref:TAPETIDE_TOKEN")
    assert tape.plan == PlanSettings(daily=400, monthly=5000)
    assert tape.plan is not None
    assert (tape.plan.min_interval_s, tape.plan.month_start_day) == (1.1, 1)
    assert tape.plan.reserve_pct == ReservePct(watchlist=10, universe=30)
    assert (bse.enabled, bse.role, bse.plan, bse.credential) == (True, "fallback", None, None)
    assert (upstox.enabled, upstox.role, upstox.plan) == (False, "primary", None)
    assert upstox.credential == "ref:UPSTOX_ANALYTICS_TOKEN"
    assert f.compare == CompareSettings(
        statements_tolerance_pct=D(1), shareholding_tolerance_pp=D("0.5")
    )
    assert f.provider("tapetide") is tape and f.provider("nope") is None


def test_the_shipped_config_block_equals_the_defaults(tmp_path: Path) -> None:
    shipped = load_settings(ROOT / "config" / "nivesh.yaml")
    assert shipped.fundamentals == FundamentalsSettings() == Settings().fundamentals
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    assert "\nfundamentals:" in text and "free plan" not in text.lower()
    assert load(tmp_path, "").fundamentals == FundamentalsSettings()  # the key may be omitted


def test_a_cli_env_style_copy_of_the_shipped_config_still_loads(
    cli_env: tuple[list[str], Path],
) -> None:
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    assert load_settings(cfg).fundamentals == FundamentalsSettings()


def test_a_custom_block_loads_and_overrides_the_defaults(tmp_path: Path) -> None:
    f = load(
        tmp_path,
        """fundamentals:
  providers:
    - name: tapetide
      plan:
        monthly: 2000
        month_start_day: 5
        min_interval_s: 0
        reserve_pct: {watchlist: 0, universe: 50}
      credential: ref:OTHER_NAME
    - {name: bse_xbrl, role: fallback}
  compare: {statements_tolerance_pct: 2.5, shareholding_tolerance_pp: 0.1}
""",
    ).fundamentals
    tape = f.providers[0]
    assert tape.plan == PlanSettings(
        monthly=2000, month_start_day=5, min_interval_s=0,
        reserve_pct=ReservePct(watchlist=0, universe=50),
    )  # fmt: skip
    assert (tape.plan.daily if tape.plan else 0) is None and tape.credential == "ref:OTHER_NAME"
    assert f.compare.statements_tolerance_pct == D("2.5")  # a YAML float stays exact
    assert f.compare.shareholding_tolerance_pp == D("0.1")
    assert [p.name for p in f.providers] == ["tapetide", "bse_xbrl"]


def test_a_literal_credential_is_refused_without_echoing_it(tmp_path: Path) -> None:
    dummy = "lit" + "eral-" + "value-1"
    text = f"fundamentals:\n  providers:\n    - {{name: tapetide, credential: {dummy}}}\n"
    with pytest.raises(ConfigError) as e:
        load(tmp_path, text)
    assert "fundamentals.providers[0].credential" in str(e.value) and dummy not in str(e.value)


def test_a_null_credential_is_refused_so_omit_the_key(tmp_path: Path) -> None:
    text = "fundamentals:\n  providers:\n    - {name: bse_xbrl, role: fallback, credential: null}\n"
    with pytest.raises(ConfigError, match="credential"):
        load(tmp_path, text)
    ok = "fundamentals:\n  providers:\n    - {name: bse_xbrl, role: fallback}\n"
    assert load(tmp_path, ok).fundamentals.providers[0].credential is None


@pytest.mark.parametrize("bad", ["ref:lower", "plain-text", "ref:A B"])
def test_the_model_itself_validates_the_reference_shape_and_never_echoes(bad: str) -> None:
    data = {"providers": [{"name": "tapetide", "credential": bad}]}
    with pytest.raises(ConfigError) as e:
        validate(FundamentalsSettings, data, Path("p.yaml"))
    assert "reference" in str(e.value) and bad not in str(e.value).split("p.yaml", 1)[1]


@pytest.mark.parametrize(
    "block",
    [
        "fundamentals: {surprise: 1}",
        "fundamentals: {compare: {surprise: 1}}",
        "fundamentals: {providers: [{name: a, surprise: 1}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, surprise: 1}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, reserve_pct: {surprise: 1}}}]}",
        "fundamentals: {providers: [{name: a}, {name: a}]}",  # duplicate name
        "fundamentals: {providers: [{name: a, role: tertiary}]}",
        "fundamentals: {providers: [{name: Tape}]}",
        "fundamentals: {providers: [{name: 1a}]}",
        "fundamentals: {providers: [{name: ''}]}",
        "fundamentals: {providers: [{name: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}]}",  # 33 chars
        "fundamentals: {providers: [{name: a, enabled: maybe}]}",
        "fundamentals: {providers: [{name: a, plan: {}}]}",  # a plan must set a cap
        "fundamentals: {providers: [{name: a, plan: {min_interval_s: 1}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 0}}]}",
        "fundamentals: {providers: [{name: a, plan: {monthly: 0}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, min_interval_s: -1}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, month_start_day: 31}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, month_start_day: 0}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, reserve_pct: {universe: 91}}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, reserve_pct: {watchlist: -1}}}]}",
        "fundamentals: {providers: [{name: a, plan: {daily: 5, reserve_pct: {watchlist: 40}}}]}",
        "fundamentals: {compare: {statements_tolerance_pct: 0}}",
        "fundamentals: {compare: {shareholding_tolerance_pp: -1}}",
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, block: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, block + "\n")


def test_a_plan_may_cap_only_one_window() -> None:
    assert PlanSettings(daily=5).monthly is None and PlanSettings(monthly=5).daily is None


def test_no_field_name_looks_sensitive_to_the_redaction_helper() -> None:
    for model in (FundamentalsSettings, ProviderSpec, PlanSettings, ReservePct, CompareSettings):
        assert not [n for n in model.model_fields if is_sensitive_key(n)], model
