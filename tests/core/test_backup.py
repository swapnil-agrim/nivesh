import shutil
import sqlite3
import stat
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core import backup
from nivesh_core.db import init_stores
from nivesh_core.errors import NiveshError
from tests import pii_values as pv

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
RECIPIENT = "age1" + "q" * 20
REAL_ENCRYPT, REAL_DECRYPT = backup._encrypt, backup._decrypt


def _fake_encrypt(src: Path, dst: Path, recipient: str) -> None:
    dst.write_bytes(src.read_bytes()[::-1])


def _fake_decrypt(src: Path, dst: Path, identity: Path) -> None:
    dst.write_bytes(src.read_bytes()[::-1])


@pytest.fixture(autouse=True)
def fake_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "_encrypt", _fake_encrypt)
    monkeypatch.setattr(backup, "_decrypt", _fake_decrypt)


def make_data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    init_stores(d)
    c = sqlite3.connect(d / "nivesh.sqlite")
    c.execute("insert into account (name, created_at) values ('main', 't')")
    c.execute("insert into security (symbol, exchange, currency) values ('X', 'NSE', 'INR')")
    c.execute("insert into run (command, started_at, status) values ('ping', 't', 'ok')")
    c.commit()
    c.close()
    (d / "runs" / "1").mkdir(parents=True)
    (d / "runs" / "1" / "trace.jsonl").write_text('{"seq": 1}\n')
    return d


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    d = make_data(tmp_path)
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    assert out.name == "nivesh-backup-20261009T120000Z.tar.age"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    fresh = tmp_path / "fresh"
    backup.restore_backup(out, fresh, tmp_path / "identity.txt")
    c = sqlite3.connect(fresh / "nivesh.sqlite")
    assert c.execute("select count(*) from account").fetchone() == (1,)
    assert c.execute("select symbol from security").fetchone() == ("X",)
    assert c.execute("select command from run").fetchone() == ("ping",)
    assert c.execute("select max(version) from schema_version").fetchone() == (2,)
    c.close()
    with duckdb.connect(str(fresh / "nivesh.duckdb")) as k:
        k.execute("select * from cache_entry")
    t = "runs/1/trace.jsonl"
    assert (fresh / t).read_bytes() == (d / t).read_bytes()
    assert stat.S_IMODE((fresh / t).stat().st_mode) == 0o600
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700


def test_no_plaintext_left_behind_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_data(tmp_path)
    seen: list[Path] = []

    def boom(src: Path, dst: Path, recipient: str) -> None:
        seen.append(src)
        raise NiveshError("age exploded")

    monkeypatch.setattr(backup, "_encrypt", boom)
    with pytest.raises(NiveshError):
        backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    assert seen and not seen[0].exists() and not seen[0].parent.exists()
    assert list((tmp_path / "bk").iterdir()) == []


def test_prune_by_name_timestamp(tmp_path: Path) -> None:
    bk = tmp_path / "bk"
    bk.mkdir()

    def name(age_days: int) -> str:
        return (
            f"nivesh-backup-{(NOW - timedelta(days=age_days)).strftime('%Y%m%dT%H%M%SZ')}.tar.age"
        )

    for n in (name(31), name(29), "notes.txt", "nivesh-backup-garbage.tar.age"):
        (bk / n).write_text("x")
    removed = backup.prune(bk, 30, NOW)
    assert [p.name for p in removed] == [name(31)]
    assert {p.name for p in bk.iterdir()} == {
        name(29),
        "notes.txt",
        "nivesh-backup-garbage.tar.age",
    }


def test_restore_refuses_foreign_and_existing_dirs(tmp_path: Path) -> None:
    d = make_data(tmp_path)
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    foreign = tmp_path / "home"
    foreign.mkdir()
    (foreign / "photo.jpg").write_text("x")
    with pytest.raises(NiveshError):
        backup.restore_backup(out, foreign, tmp_path / "id")
    with pytest.raises(NiveshError, match="already holds"):
        backup.restore_backup(out, d, tmp_path / "id")


def test_garbled_archive_raises_and_leaves_target_untouched(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar.age"
    bad.write_bytes(b"not a tar at all" * 50)
    target = tmp_path / "t"
    with pytest.raises(NiveshError):
        backup.restore_backup(bad, target, tmp_path / "id")
    assert not target.exists()


def evil_archive(tmp_path: Path, member: tarfile.TarInfo, payload: bytes = b"") -> Path:
    import io

    plain = tmp_path / "evil.tar"
    with tarfile.open(plain, "w") as tf:
        tf.addfile(member, io.BytesIO(payload))
    out = tmp_path / "evil.tar.age"
    _fake_encrypt(plain, out, RECIPIENT)
    return out


def test_path_traversal_and_links_rejected(tmp_path: Path) -> None:
    trav = tarfile.TarInfo("../escaped.txt")
    trav.size = 3
    target = tmp_path / "restored"
    with pytest.raises(NiveshError, match="unsafe"):
        backup.restore_backup(evil_archive(tmp_path, trav, b"abc"), target, tmp_path / "id")
    assert not (tmp_path / "escaped.txt").exists() and not target.exists()
    link = tarfile.TarInfo("nivesh.sqlite")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    with pytest.raises(NiveshError, match="unsafe"):
        backup.restore_backup(evil_archive(tmp_path, link), target, tmp_path / "id")


def test_bad_recipient_rejected_before_calling_age() -> None:
    with pytest.raises(NiveshError, match="age public key"):
        REAL_ENCRYPT(Path("a"), Path("b"), "-o /etc/passwd")


def cli_cfg(tmp_path: Path, target: bool = True) -> list[str]:
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    text = (ROOT / "config" / "nivesh.yaml").read_text().replace("data_dir: data", "data_dir: data")
    if target:
        text += f"backup: {{target: {tmp_path / 'bk'}, recipient: {RECIPIENT}}}\n"
    (cfg / "nivesh.yaml").write_text(text)
    (cfg / "profile.yaml").write_text((ROOT / "config" / "profile.yaml").read_text())
    return ["--config-dir", str(cfg)]


def test_cli_backup_and_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    make_data(tmp_path)
    cfg = cli_cfg(tmp_path)
    r = CliRunner().invoke(app, [*cfg, "backup"])
    assert r.exit_code == 0, r.output
    (arch,) = list((tmp_path / "bk").glob("nivesh-backup-*.tar.age"))
    ident = tmp_path / "ident.txt"
    ident.write_text("x")
    r = CliRunner().invoke(
        app,
        [*cfg, "restore", str(arch), "--identity", str(ident), "--data-dir", str(tmp_path / "r")],
    )
    assert r.exit_code == 0, r.output
    assert (tmp_path / "r" / "nivesh.sqlite").exists()


def test_cli_backup_needs_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    make_data(tmp_path)
    r = CliRunner().invoke(app, [*cli_cfg(tmp_path, target=False), "backup"])
    assert r.exit_code == 1 and "backup.target" in r.output


@pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")
def test_real_age_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "_encrypt", REAL_ENCRYPT)
    monkeypatch.setattr(backup, "_decrypt", REAL_DECRYPT)
    ident = tmp_path / "ident.txt"
    out = subprocess.run(["age-keygen", "-o", str(ident)], capture_output=True, text=True)  # noqa: S603,S607
    recipient = (out.stderr + out.stdout).split("Public key:")[1].split()[0]
    d = make_data(tmp_path)
    arch = backup.create_backup(d, tmp_path / "bk", recipient, NOW)
    backup.restore_backup(arch, tmp_path / "fresh", ident)
    assert (tmp_path / "fresh" / "runs" / "1" / "trace.jsonl").exists()


def test_trace_content_survives_with_pii_free_bytes(tmp_path: Path) -> None:
    # backups hold run dirs verbatim; PII was already redacted when traces were written
    d = make_data(tmp_path)
    (d / "runs" / "1" / "trace.jsonl").write_text("[REDACTED]\n")
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    assert pv.pan().encode() not in out.read_bytes()


@pytest.mark.parametrize("name", ["nivesh.duckdb", "runs"])
def test_restore_refuses_any_existing_store(tmp_path: Path, name: str) -> None:
    out = backup.create_backup(make_data(tmp_path), tmp_path / "bk", RECIPIENT, NOW)
    t = tmp_path / "t"
    t.mkdir()
    (t / name).mkdir()
    with pytest.raises(NiveshError, match="already holds"):
        backup.restore_backup(out, t, tmp_path / "id")


def test_decrypt_argv_guards_dash_named_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(backup, "_age", seen.append)
    monkeypatch.chdir(tmp_path)
    REAL_DECRYPT(Path("-x.age"), tmp_path / "o", tmp_path / "id")
    assert seen[0][-2] == "--" and seen[0][-1] == str((tmp_path / "-x.age").resolve())


def test_restore_rolls_back_when_final_placement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_data(tmp_path)
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    fresh = tmp_path / "fresh"
    real, calls = shutil.move, []

    def flaky(src: str, dst: str) -> str:
        calls.append(src)
        if len(calls) == 2:  # runs placed, then the database move fails
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(backup.shutil, "move", flaky)
    with pytest.raises(OSError, match="disk full"):
        backup.restore_backup(out, fresh, tmp_path / "id")
    assert not fresh.exists()
    monkeypatch.setattr(backup.shutil, "move", real)
    backup.restore_backup(out, fresh, tmp_path / "id")
    assert (fresh / "nivesh.sqlite").exists() and (fresh / "runs" / "1").is_dir()
