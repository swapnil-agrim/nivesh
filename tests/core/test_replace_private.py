import stat
from pathlib import Path

import pytest

from nivesh_core.errors import ConfigError
from nivesh_core.paths import replace_private


def test_replace_private_overwrites_existing_file_with_0600(tmp_path: Path) -> None:
    f = tmp_path / "t.json"
    replace_private(f, "one")
    replace_private(f, "two")
    assert f.read_text() == "two" and stat.S_IMODE(f.stat().st_mode) == 0o600


def test_replace_private_refuses_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match="symlink"):
        replace_private(link, "x")
    assert real.read_text() == "keep"


def test_replace_private_leaves_no_temp_file(tmp_path: Path) -> None:
    f = tmp_path / "t.json"
    (tmp_path / "t.json.tmp").write_text("stale")
    replace_private(f, "ok")
    assert [p.name for p in tmp_path.iterdir()] == ["t.json"]
