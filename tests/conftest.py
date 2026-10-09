import socket
from collections.abc import Iterator
from typing import Any

import pytest


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
