from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.config import load_settings
from nivesh_core.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return p


def test_sample_settings_and_nfr7_ttls() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.mode == "dev"
    assert s.ttls.price == timedelta(days=1)
    assert s.ttls.fundamentals == timedelta(days=7)
    assert s.ttls.nav == timedelta(days=1)
    assert s.ttls.mf_holdings == timedelta(days=31)


def test_defaults(tmp_path: Path) -> None:
    s = load_settings(write(tmp_path, "data_dir: x\n"))
    assert s.mode == "dev"
    assert s.ttls.mf_holdings == timedelta(days=31)


@pytest.mark.parametrize(
    "text",
    [
        "mode: staging\n",
        "ttls: {price: soon}\n",
        "unknown_field: 1\n",
    ],
)
def test_invalid(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


@pytest.mark.parametrize("ref", ["ref:ANTHROPIC_API_KEY", "ref:MY_TOKEN_2"])
def test_valid_refs(tmp_path: Path, ref: str) -> None:
    s = load_settings(write(tmp_path, f"anthropic_api_key: {ref}\n"))
    assert s.anthropic_api_key == ref


@pytest.mark.parametrize(
    "line",
    [
        "anthropic_api_key: sk-ant-abc123",
        "anthropic_api_key: ref:lowercase",
        "anthropic_api_key: 'ref: SPACE'",
        "anthropic_api_key: ''",
        "ttls: {price: 1d, db_password: hunter2}",
        "nested: {broker_token: abc}",
    ],
)
def test_literal_secrets_rejected_without_echo(tmp_path: Path, line: str) -> None:
    with pytest.raises(ConfigError) as ei:
        load_settings(write(tmp_path, line + "\n"))
    msg = str(ei.value)
    assert "ref:" in msg  # tells the user the expected format
    for leaked in ("sk-ant-abc123", "hunter2"):
        assert leaked not in msg


def test_new_ops_fields_have_defaults_and_validate(tmp_path: Path) -> None:
    s = load_settings(write(tmp_path, "data_dir: x\n"))
    assert s.egress_url == "https://api.ipify.org" and s.registered_ip is None
    assert s.backup.retention_days == 30 and s.backup.target is None and s.usd_inr > 0
    s = load_settings(
        write(
            tmp_path,
            "prices: {m: {input_usd_per_mtok: 3, output_usd_per_mtok: 15}}\n"
            "usd_inr: 90\nregistered_ip: 203.0.113.7\n"
            "backup: {target: /b, recipient: age1abc, retention_days: 7}\n",
        )
    )
    assert s.prices["m"].output_usd_per_mtok == 15 and s.registered_ip == "203.0.113.7"
    assert s.backup.retention_days == 7


@pytest.mark.parametrize(
    "text, field",
    [
        ("prices: {m: {input_usd_per_mtok: -1, output_usd_per_mtok: 1}}\n", "input_usd_per_mtok"),
        ("usd_inr: 0\n", "usd_inr"),
        ("registered_ip: not-an-ip\n", "registered_ip"),
        ("backup: {surprise: 1}\n", "surprise"),
    ],
)
def test_new_ops_fields_rejected_with_field_path(tmp_path: Path, text: str, field: str) -> None:
    with pytest.raises(ConfigError, match=field):
        load_settings(write(tmp_path, text))


def test_investright_block_defaults(tmp_path: Path) -> None:
    ir = load_settings(write(tmp_path, "data_dir: x\n")).investright
    assert ir.redirect_port == 8765 and ir.base_url.startswith("https://")
    assert ir.app_key == "ref:INVESTRIGHT_API_KEY" and ir.api_secret == "ref:INVESTRIGHT_API_SECRET"
    assert ir.demat_ref is None
    assert load_settings(ROOT / "config" / "nivesh.yaml").investright.redirect_port == 8765


def test_investright_literal_api_secret_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ref:"):
        load_settings(write(tmp_path, "investright: {api_secret: plain}\n"))


def test_investright_base_url_must_be_https(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        load_settings(write(tmp_path, "investright: {base_url: http://example.test}\n"))


@pytest.mark.parametrize("port", [80, 70000])
def test_investright_redirect_port_range(tmp_path: Path, port: int) -> None:
    with pytest.raises(ConfigError, match="redirect_port"):
        load_settings(write(tmp_path, f"investright: {{redirect_port: {port}}}\n"))


def test_new_ttl_defaults_news_macro_filings_estimates_master() -> None:
    t = load_settings(ROOT / "config" / "nivesh.yaml").ttls
    assert (t.news, t.macro, t.filings, t.estimates, t.master) == (
        timedelta(hours=6), timedelta(days=1), timedelta(days=7), timedelta(days=1),
        timedelta(days=7),
    )  # fmt: skip


def test_sample_config_loads_with_market_block() -> None:
    m = load_settings(ROOT / "config" / "nivesh.yaml").market
    assert m.edgar_max_per_sec <= 10 and m.macro_series["usdinr"].source == "fred"
    assert 2026 in m.nse_holidays and m.feeds and m.cross_check_tolerance == Decimal("0.01")


def test_market_defaults_present_when_block_absent(tmp_path: Path) -> None:
    m = load_settings(write(tmp_path, "data_dir: x\n")).market
    assert m.edgar_max_per_sec == 8 and m.us_secondary == "stooq"
    assert m.fred_api_key == "ref:FRED_API_KEY" and m.fmp_api_key == "ref:FMP_API_KEY"
    assert m.macro_series == {} and m.feeds == [] and m.nse_holidays == {}


def test_market_secret_fields_must_be_refs(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="fmp_api_key"):
        load_settings(write(tmp_path, "market: {fmp_api_key: lowercase}\n"))


def test_market_literal_fred_key_rejected_without_echoing_value(tmp_path: Path) -> None:
    from tests import pii_values as pv

    dummy = pv.fred_key()
    with pytest.raises(ConfigError) as ei:
        load_settings(write(tmp_path, f"market: {{fred_api_key: {dummy}}}\n"))
    assert dummy not in str(ei.value) and "ref:" in str(ei.value)


@pytest.mark.parametrize("rate", [0, 11])
def test_edgar_rate_must_be_at_most_ten_per_second(tmp_path: Path, rate: int) -> None:
    with pytest.raises(ConfigError, match="edgar_max_per_sec"):
        load_settings(write(tmp_path, f"market: {{edgar_max_per_sec: {rate}}}\n"))


@pytest.mark.parametrize("tol", [0, 0.6])
def test_cross_check_tolerance_range(tmp_path: Path, tol: float) -> None:
    with pytest.raises(ConfigError, match="cross_check_tolerance"):
        load_settings(write(tmp_path, f"market: {{cross_check_tolerance: {tol}}}\n"))


def test_cross_check_tolerance_yaml_float_is_exact_decimal(tmp_path: Path) -> None:
    m = load_settings(write(tmp_path, "market: {cross_check_tolerance: 0.07}\n")).market
    assert m.cross_check_tolerance == Decimal("0.07")


def test_nse_holiday_year_must_match_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nse_holidays"):
        load_settings(write(tmp_path, "market: {nse_holidays: {2026: [2025-01-26]}}\n"))


def test_feed_url_must_be_https(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="url"):
        load_settings(write(tmp_path, "market: {feeds: [{name: a, url: 'http://x.test/f'}]}\n"))


def test_macro_role_names_are_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="macro_series"):
        load_settings(write(tmp_path, "market: {macro_series: {bogus: {source: fred, id: X}}}\n"))


def test_tax_block_defaults_none_and_must_be_positive(tmp_path: Path) -> None:
    assert load_settings(write(tmp_path, "data_dir: x\n")).tax.us_long_term_days is None
    ok = load_settings(write(tmp_path, "tax: {us_long_term_days: 365}\n"))
    assert ok.tax.us_long_term_days == 365
    for bad in ("0", "-5", "abc"):
        with pytest.raises(ConfigError):
            load_settings(write(tmp_path, f"tax: {{us_long_term_days: {bad}}}\n"))
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, "tax: {rate: 0.2}\n"))


def test_sample_config_sets_tax_param_and_loads() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.tax.us_long_term_days == 365
    assert "gives no tax advice" in (ROOT / "config" / "nivesh.yaml").read_text()


def test_us_broker_defaults_and_https_url(tmp_path: Path) -> None:
    ub = load_settings(write(tmp_path, "data_dir: x\n")).us_broker
    assert ub.alpaca_key == "ref:ALPACA_KEY" and ub.alpaca_secret == "ref:ALPACA_SECRET"
    assert ub.base_url.startswith("https://")
    with pytest.raises(ConfigError, match="https"):
        load_settings(write(tmp_path, "us_broker: {base_url: http://x.test}\n"))
    ok = load_settings(write(tmp_path, "us_broker: {base_url: 'https://x.test/'}\n"))
    assert ok.us_broker.base_url == "https://x.test"


def test_us_broker_fields_must_be_refs(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, "us_broker: {alpaca_key: not-a-ref}\n"))


def test_literal_alpaca_secret_rejected_without_echoing_value(tmp_path: Path) -> None:
    value = "test-" + "alpaca-" + "sec"
    with pytest.raises(ConfigError) as ei:
        load_settings(write(tmp_path, f"us_broker: {{alpaca_secret: {value}}}\n"))
    assert value not in str(ei.value) and "us_broker.alpaca_secret" in str(ei.value)
    with pytest.raises(ConfigError) as ei:
        load_settings(write(tmp_path, f"us_broker: {{alpaca_key: {value}}}\n"))
    assert value not in str(ei.value)


def test_sample_config_loads_with_us_broker_block() -> None:
    s = load_settings(ROOT / "config" / "nivesh.yaml")
    assert s.us_broker.alpaca_key == "ref:ALPACA_KEY"
