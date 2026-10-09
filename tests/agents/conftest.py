from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _cwd_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`nivesh run` now writes run rows and trace dirs under config data_dir ("data", relative);
    pin cwd to tmp so the repo's own data/ is never touched."""
    monkeypatch.chdir(tmp_path)
