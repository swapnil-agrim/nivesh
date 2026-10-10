from pathlib import Path

import pytest

import nivesh_mcp.common as common
from tests.mcp.mkt_fx import NOW, Mkt, seed_master


@pytest.fixture
def mkt(cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch) -> Mkt:
    """A tmp data dir with a seeded security master; the server clock is fixed to `NOW`."""
    args, data = cli_env
    monkeypatch.setenv("NIVESH_CONFIG_DIR", args[1])
    monkeypatch.setattr(common, "now", lambda: NOW)
    common._ready.clear()
    return Mkt(data, seed_master(data))
