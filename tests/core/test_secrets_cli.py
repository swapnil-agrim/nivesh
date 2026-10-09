import os
import stat
from pathlib import Path

import keyring
import pytest
from keyring.errors import NoKeyringError
from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core.errors import NiveshError
from nivesh_core.paths import write_private
from nivesh_core.secrets import get_secret, secret_exists, set_secret

ROOT = Path(__file__).resolve().parents[2]
CFG = ["--config-dir", str(ROOT / "config")]
VALUE = "hunter" + "-value-" + "42"
runner = CliRunner()


def test_set_prompts_hidden_and_stores(
    fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    r = runner.invoke(app, [*CFG, "secrets", "set", "MY_KEY"], input=VALUE + "\n")
    assert r.exit_code == 0, r.output
    assert VALUE not in r.output
    assert keyring.get_password("nivesh", "MY_KEY") == VALUE
    assert get_secret("MY_KEY") == VALUE


def test_set_has_no_value_argument(fake_keyring: object) -> None:
    r = runner.invoke(app, [*CFG, "secrets", "set", "MY_KEY", VALUE])
    assert r.exit_code != 0
    assert keyring.get_password("nivesh", "MY_KEY") is None


@pytest.mark.parametrize("bad", ["lower", "1ABC", "A-B", "A" * 70])
def test_invalid_name_exits_1(fake_keyring: object, bad: str) -> None:
    r = runner.invoke(app, [*CFG, "secrets", "set", bad], input=VALUE + "\n")
    assert r.exit_code == 1
    assert VALUE not in r.output


def test_check_never_prints_value(fake_keyring: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    assert "missing" in runner.invoke(app, [*CFG, "secrets", "check", "MY_KEY"]).output
    set_secret("MY_KEY", VALUE)
    r = runner.invoke(app, [*CFG, "secrets", "check", "MY_KEY"])
    assert "set" in r.output and VALUE not in r.output
    assert secret_exists("MY_KEY")


def test_no_backend_exits_1_pointing_at_env(
    fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: str) -> None:
        raise NoKeyringError("none")

    monkeypatch.setattr(keyring, "set_password", boom)
    r = runner.invoke(app, [*CFG, "secrets", "set", "MY_KEY"], input=VALUE + "\n")
    assert r.exit_code == 1
    assert "environment" in r.output and VALUE not in r.output


def test_set_secret_rejects_bad_name_and_empty(fake_keyring: object) -> None:
    with pytest.raises(NiveshError):
        set_secret("bad name", VALUE)
    with pytest.raises(NiveshError):
        set_secret("MY_KEY", "")


def test_write_private_is_0600_from_birth(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        p = tmp_path / "f"
        write_private(p, "hello")
    finally:
        os.umask(old)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and p.read_text() == "hello"


def test_write_private_refuses_symlink_and_existing(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    with pytest.raises(OSError):
        write_private(link, "y")
    with pytest.raises(OSError):
        write_private(link, "y", append=True)
    with pytest.raises(OSError):
        write_private(target, "y")
    assert target.read_text() == "x"


def test_write_private_append(tmp_path: Path) -> None:
    p = tmp_path / "a"
    write_private(p, "1", append=True)
    write_private(p, "2", append=True)
    assert p.read_text() == "12" and stat.S_IMODE(p.stat().st_mode) == 0o600
