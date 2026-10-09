import logging

import pytest

from nivesh_core import secrets
from nivesh_core.errors import ConfigError, SecretNotFound


def test_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET_X", "from-env")
    monkeypatch.setattr(secrets.keyring, "get_password", lambda *a: "from-keyring")
    assert secrets.get_secret("MY_SECRET_X") == "from-env"


def test_keyring_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_SECRET_X", raising=False)
    calls: list[tuple[str, str]] = []

    def fake(service: str, name: str) -> str:
        calls.append((service, name))
        return "from-keyring"

    monkeypatch.setattr(secrets.keyring, "get_password", fake)
    assert secrets.get_secret("MY_SECRET_X") == "from-keyring"
    assert calls == [("nivesh", "MY_SECRET_X")]


def test_not_found_and_no_leak(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("MY_SECRET_X", raising=False)
    monkeypatch.setattr(secrets.keyring, "get_password", lambda *a: None)
    with pytest.raises(SecretNotFound, match="MY_SECRET_X"):
        secrets.get_secret("MY_SECRET_X")


def test_keyring_backend_failure_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_SECRET_X", raising=False)

    def boom(*a: str) -> str:
        raise RuntimeError("no backend")

    monkeypatch.setattr(secrets.keyring, "get_password", boom)
    with pytest.raises(SecretNotFound):
        secrets.get_secret("MY_SECRET_X")


def test_secret_never_in_repr_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MY_SECRET_X", "s3cr3t-value")
    with caplog.at_level(logging.DEBUG):
        s = secrets.get_secret("MY_SECRET_X")
    assert s == "s3cr3t-value"
    assert "s3cr3t-value" not in caplog.text
    ref = secrets.SecretRef("ref:MY_SECRET_X")
    assert "s3cr3t-value" not in repr(ref) and "MY_SECRET_X" in repr(ref)
    assert ref.resolve() == "s3cr3t-value"


def test_bad_ref() -> None:
    with pytest.raises(ConfigError):
        secrets.SecretRef("literal")
