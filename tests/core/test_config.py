from datetime import timedelta
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
