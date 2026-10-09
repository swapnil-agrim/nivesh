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
