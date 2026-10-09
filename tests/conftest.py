import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend


@pytest.fixture(autouse=True)
def _no_network_and_replay_mode(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Unit tests never touch the network: real connects fail, adapters replay fixtures."""
    if request.node.get_closest_marker("live"):
        yield
        return
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX:  # local IPC (asyncio, subprocess pipes) is fine
            return real_connect(self, address)
        raise RuntimeError(f"network access blocked in tests: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: 1)
    monkeypatch.setenv("NIVESH_REPLAY", "1")
    monkeypatch.delenv("NIVESH_RECORD", raising=False)
    monkeypatch.delenv("NIVESH_REFRESH", raising=False)
    yield


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, pw: str) -> None:
        self.store[(service, username)] = pw

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring() -> Iterator[MemoryKeyring]:
    """In-memory keychain; the real OS keychain is never touched."""
    old = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(old)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    """(global CLI args, data_dir): a tmp config dir whose data_dir is inside tmp_path."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    data = tmp_path / "data"
    text = (
        (ROOT / "config" / "nivesh.yaml").read_text().replace("data_dir: data", f"data_dir: {data}")
    )
    (cfg / "nivesh.yaml").write_text(text)
    shutil.copy(ROOT / "config" / "profile.yaml", cfg / "profile.yaml")
    for name in ("INVESTRIGHT_API_KEY", "INVESTRIGHT_API_SECRET", "CAS_PASSWORD", "FOLIO_SALT"):
        monkeypatch.delenv(name, raising=False)
    return ["--config-dir", str(cfg)], data
