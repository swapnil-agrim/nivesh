import re
from pathlib import Path

import pytest

from nivesh_core.config import Settings, load_settings
from nivesh_core.delivery_config import DeliverySettings
from nivesh_core.errors import ConfigError
from nivesh_core.redact import is_sensitive_key

ROOT = Path(__file__).resolve().parents[2]
REF_FIELDS = ("telegram_bot", "telegram_chat", "slack_hook", "smtp_host", "smtp_user",
              "smtp_auth", "mail_to")  # fmt: skip


def load(tmp_path: Path, text: str) -> Settings:
    p = tmp_path / "nivesh.yaml"
    p.write_text(text)
    return load_settings(p)


def test_defaults_validate_without_delivery_block_and_send_nothing(tmp_path: Path) -> None:
    s = load(tmp_path, "data_dir: x\n")
    assert s.delivery == DeliverySettings() and s.delivery.channels == []
    assert s.delivery.max_attempts == 3 and s.delivery.timeout_s == 10


def test_example_block_equals_defaults_and_contains_only_ref_names() -> None:
    path = ROOT / "config" / "nivesh.yaml"
    assert load_settings(path).delivery == DeliverySettings()
    block = path.read_text().split("\ndelivery:", 1)[1]
    values = re.findall(r":\s*(ref:\S+|\S+)\s*$", block, re.M)
    assert [v for v in values if v.startswith("ref:") and not re.fullmatch(r"ref:[A-Z_]+", v)] == []
    assert not re.search(r"@|https?://", block)


def test_literal_value_in_a_reference_field_rejected(tmp_path: Path) -> None:
    dummy = "lit" + "eral" + "-value-" + "xyz"
    for field in REF_FIELDS:
        with pytest.raises(ConfigError) as e:
            load(tmp_path, f"delivery: {{{field}: {dummy}}}\n")
        assert dummy not in str(e.value)
        ok = load(tmp_path, f"delivery: {{{field}: 'ref:SOME_NAME'}}\n").delivery
        assert getattr(ok, field) == "ref:SOME_NAME"


def test_unknown_channel_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, "delivery: {channels: [fax]}\n")
    assert load(tmp_path, "delivery: {channels: [slack, email]}\n").delivery.channels == [
        "slack", "email",
    ]  # fmt: skip
    with pytest.raises(ConfigError):
        load(tmp_path, "delivery: {surprise: 1}\n")


def test_max_attempts_bounded_1_to_5(tmp_path: Path) -> None:
    for bad in ("max_attempts: 0", "max_attempts: 6", "timeout_s: 0", "timeout_s: 61"):
        with pytest.raises(ConfigError):
            load(tmp_path, f"delivery: {{{bad}}}\n")
    assert load(tmp_path, "delivery: {max_attempts: 5}\n").delivery.max_attempts == 5


def test_no_field_name_looks_like_a_credential_or_is_sensitive() -> None:
    names = list(DeliverySettings.model_fields)
    assert set(REF_FIELDS) <= set(names)
    assert [n for n in names if is_sensitive_key(n)] == []
    assert not [n for n in names if re.search("secret|token|password|credential|key|private", n)]


def test_recipients_are_refs_not_email_literals(tmp_path: Path) -> None:
    lit = "someone" + "@" + "example" + ".org"
    with pytest.raises(ConfigError) as e:
        load(tmp_path, f"delivery: {{mail_to: {lit}}}\n")
    assert lit not in str(e.value)
