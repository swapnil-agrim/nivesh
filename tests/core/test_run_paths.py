"""Dated run directories (`runs/<IST date>/<run id>`) and the resolver that keeps legacy ones."""

import json
import re
import sqlite3
import stat
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nivesh_cli.common as common
from nivesh_cli.common import metered_run
from nivesh_cli.main import app
from nivesh_cli.review import review_flags
from nivesh_core import backup
from nivesh_core.errors import NiveshError
from nivesh_core.paths import find_run_dir, run_dir
from tests.cli.test_metered_run import loaded
from tests.run_paths import run_col

DAY = date(2026, 10, 12)


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_run_dir_with_day_is_runs_date_id_and_owner_only(tmp_path: Path) -> None:
    d = run_dir(tmp_path, 7, DAY)
    assert d == tmp_path / "runs" / "2026-10-12" / "7"
    assert mode(d) == 0o700 and mode(d.parent) == 0o700 and mode(d.parent.parent) == 0o700


def test_run_dir_without_day_keeps_legacy_shape(tmp_path: Path) -> None:
    assert run_dir(tmp_path, 7) == tmp_path / "runs" / "7"


def test_find_run_dir_prefers_dated_then_legacy_then_none(tmp_path: Path) -> None:
    assert find_run_dir(tmp_path, 3) is None
    legacy = run_dir(tmp_path, 3)
    assert find_run_dir(tmp_path, 3) == legacy
    dated = run_dir(tmp_path, 3, DAY)
    assert find_run_dir(tmp_path, 3) == dated  # the dated one wins over a legacy twin
    assert find_run_dir(tmp_path, 4) is None
    (tmp_path / "runs" / "2026-10-12" / "outputs").mkdir()
    assert find_run_dir(tmp_path, 9) is None  # sub-folders of a day are not runs


def test_find_run_dir_takes_an_integer_only(tmp_path: Path) -> None:
    for bad in (True, 0, -1, "1", "../x", 1.0):
        with pytest.raises(NiveshError):
            find_run_dir(tmp_path, bad)  # type: ignore[arg-type]


def make_trace(d: Path) -> None:
    (d / "trace.jsonl").write_text(json.dumps({"seq": 1, "type": "start"}) + "\n")


def test_replay_reads_a_dated_run_and_a_legacy_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    root = Path(__file__).resolve().parents[2]
    (cfg / "nivesh.yaml").write_text(
        (root / "config" / "nivesh.yaml").read_text().replace("data_dir: data", "data_dir: dd")
    )
    (cfg / "profile.yaml").write_text((root / "config" / "profile.yaml").read_text())
    for rid, day in ((1, DAY), (2, None)):
        make_trace(run_dir(tmp_path / "dd", rid, day))
    for rid in ("1", "2"):
        r = CliRunner().invoke(app, ["--config-dir", str(cfg), "replay", rid])
        assert r.exit_code == 0 and "no engine steps to replay" in r.output, r.output


def test_review_flags_reads_a_dated_run(tmp_path: Path) -> None:
    out = run_dir(tmp_path, 5, DAY) / "outputs"
    out.mkdir()
    with pytest.raises(NiveshError, match="no saved holding reviews"):
        review_flags(tmp_path, 5)
    (out / "001_holding_review_1.json").write_text("{}")
    flags, notes = review_flags(tmp_path, 5)
    assert flags == {} and "not a valid holding review" in notes[0]


def test_metered_run_stores_dated_run_dir_column(
    cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, profile = loaded(cli_env)
    instant = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)  # 01:30 IST on the 12th
    monkeypatch.setattr(common, "utcnow", lambda: instant)
    with metered_run(settings, profile, "probe", "quick", force=False) as m:
        m.status = "ok"
    assert m.run_dir == cli_env[1] / "runs" / "2026-10-12" / "1"
    c = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    try:
        (col,) = c.execute("select run_dir from run").fetchone()
    finally:
        c.close()
    assert col == "runs/2026-10-12/1" and re.fullmatch(run_col(1), col)


def test_backup_copies_dated_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "_encrypt", lambda s, d, r: d.write_bytes(s.read_bytes()[::-1]))
    monkeypatch.setattr(backup, "_decrypt", lambda s, d, i: d.write_bytes(s.read_bytes()[::-1]))
    from nivesh_core.db import init_stores

    data = tmp_path / "data"
    init_stores(data)
    make_trace(run_dir(data, 1, DAY))
    out = backup.create_backup(
        data, tmp_path / "bk", "age1" + "q" * 20, datetime(2026, 10, 9, tzinfo=UTC)
    )
    fresh = tmp_path / "fresh"
    backup.restore_backup(out, fresh, tmp_path / "identity.txt")
    t = "runs/2026-10-12/1/trace.jsonl"
    assert (fresh / t).read_bytes() == (data / t).read_bytes()
