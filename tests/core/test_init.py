import os
import sqlite3
import stat
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from nivesh_core.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]
CFG = str(ROOT / "config")
runner = CliRunner()


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_init_creates_private_stores(tmp_path: Path) -> None:
    d = tmp_path / "data"
    r = runner.invoke(app, ["--config-dir", CFG, "init", "--data-dir", str(d)])
    assert r.exit_code == 0, r.output
    assert mode(d) == 0o700
    for name in ("nivesh.sqlite", "nivesh.duckdb"):
        assert mode(d / name) == 0o600
    s = sqlite3.connect(d / "nivesh.sqlite")
    latest = migrate.latest_version(MIGRATIONS / "sqlite")
    assert s.execute("select max(version) from schema_version").fetchone() == (latest,)
    s.close()
    with duckdb.connect(str(d / "nivesh.duckdb")) as k:
        assert k.execute("select max(version) from schema_version").fetchone() == (1,)
        k.execute("select * from cache_entry")


def test_every_file_in_data_dir_is_owner_only(tmp_path: Path) -> None:
    d = tmp_path / "data"
    init_stores(d)
    for p in d.iterdir():
        assert mode(p) & 0o077 == 0, p.name


def test_sqlite_sidecars_are_private_while_open(tmp_path: Path) -> None:
    from nivesh_core.db.sqlite import open_sqlite

    d = tmp_path / "data"
    init_stores(d)
    c = open_sqlite(d / "nivesh.sqlite")
    c.execute("insert into run (command, started_at, status) values ('x', 't', 'ok')")
    sidecars = [p for p in d.iterdir() if p.name.startswith("nivesh.sqlite-")]
    assert sidecars  # WAL mode
    assert all(mode(p) & 0o077 == 0 for p in sidecars)
    c.close()


def test_init_idempotent(tmp_path: Path) -> None:
    d = tmp_path / "data"
    init_stores(d)
    c = sqlite3.connect(d / "nivesh.sqlite")
    c.execute("insert into run (command, started_at, status) values ('x', 't', 'ok')")
    c.commit()
    c.close()
    r = runner.invoke(app, ["--config-dir", CFG, "init", "--data-dir", str(d)])
    assert r.exit_code == 0, r.output
    c = sqlite3.connect(d / "nivesh.sqlite")
    assert c.execute("select count(*) from run").fetchone() == (1,)
    c.close()


def test_refuses_foreign_non_empty_dir_and_leaves_perms(tmp_path: Path) -> None:
    d = tmp_path / "home"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    (d / "photo.jpg").write_text("x")
    with pytest.raises(ConfigError, match="not empty"):
        init_stores(d)
    assert mode(d) == 0o755
    r = runner.invoke(app, ["--config-dir", CFG, "init", "--data-dir", str(d)])
    assert r.exit_code != 0


def test_existing_empty_dir_is_tightened(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    os.chmod(d, 0o755)
    init_stores(d)
    assert mode(d) == 0o700


def test_invalid_profile_fails_cli_with_clear_message(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "nivesh.yaml").write_text((ROOT / "config" / "nivesh.yaml").read_text())
    (cfg / "profile.yaml").write_text(
        (ROOT / "config" / "profile.yaml")
        .read_text()
        .replace("max_position_pct: 10", "max_position_pct: 150")
    )
    r = runner.invoke(app, ["--config-dir", str(cfg), "init", "--data-dir", str(tmp_path / "d")])
    assert r.exit_code != 0
    assert "max_position_pct" in r.output
    assert "Traceback" not in r.output


def test_run_dir_is_private(tmp_path: Path) -> None:
    from nivesh_core.paths import run_dir

    d = run_dir(tmp_path, 7)
    assert d == tmp_path / "runs" / "7" and mode(d) == 0o700 and mode(d.parent) == 0o700
