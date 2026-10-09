"""Encrypted backup and restore (ST-13.6). Archive = tar of a consistent SQLite snapshot, the
checkpointed DuckDB file and runs/, encrypted with `age` to a public-key recipient. The identity
(private key) is only ever a `restore --identity` path; Nivesh never stores it.
"""

import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import duckdb

from nivesh_core.errors import NiveshError
from nivesh_core.paths import ensure_data_dir, private_umask
from nivesh_core.redact import redact_text

_STAMP = "%Y%m%dT%H%M%SZ"
NAME_RE = re.compile(r"^nivesh-backup-(\d{8}T\d{6}Z)\.tar\.age$")
_RECIPIENT_RE = re.compile(r"^age1[a-z0-9]+$")
_DB_FILES = ("nivesh.sqlite", "nivesh.duckdb")


def _age(args: list[str]) -> None:
    try:
        # fixed argv list, no shell; `age` is looked up on PATH on purpose (installed by the image)
        subprocess.run(["age", *args], check=True, capture_output=True)  # noqa: S603,S607
    except FileNotFoundError:
        raise NiveshError("the `age` binary is not installed; install age to back up") from None
    except subprocess.CalledProcessError as e:
        raise NiveshError(f"age failed: {redact_text(e.stderr.decode(errors='replace'))}") from None


def _encrypt(src: Path, dst: Path, recipient: str) -> None:
    if not _RECIPIENT_RE.match(recipient):
        raise NiveshError("backup recipient must be an age public key (age1...)")
    _age(["-r", recipient, "-o", str(dst), str(src)])


def _decrypt(src: Path, dst: Path, identity: Path) -> None:
    _age(["-d", "-i", str(identity.resolve()), "-o", str(dst), "--", str(src.resolve())])


def _snapshot(data_dir: Path, into: Path) -> None:
    sq = data_dir / "nivesh.sqlite"
    if sq.exists():
        src, dst = sqlite3.connect(sq), sqlite3.connect(into / sq.name)
        try:
            src.backup(dst)  # consistent even with WAL sidecars present
        finally:
            dst.close()
            src.close()
    dk = data_dir / "nivesh.duckdb"
    if dk.exists():
        con = duckdb.connect(str(dk))
        try:
            con.execute("CHECKPOINT")
        finally:
            con.close()
        shutil.copy2(dk, into / dk.name)
    if (data_dir / "runs").is_dir():
        shutil.copytree(data_dir / "runs", into / "runs")


def create_backup(data_dir: Path, target_dir: Path, recipient: str, now: datetime) -> Path:
    with private_umask():
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"nivesh-backup-{now.astimezone(UTC).strftime(_STAMP)}.tar.age"
        with tempfile.TemporaryDirectory() as tmp:  # 0700; removed even on failure
            stage = Path(tmp) / "stage"
            stage.mkdir()
            _snapshot(data_dir, stage)
            plain = Path(tmp) / "backup.tar"
            with tarfile.open(plain, "w") as tf:
                for item in sorted(stage.iterdir()):
                    tf.add(item, arcname=item.name)
            _encrypt(plain, dest, recipient)
    os.chmod(dest, 0o600)
    return dest


def prune(target_dir: Path, retention_days: int, now: datetime) -> list[Path]:
    """Delete backup archives older than `retention_days` (by the timestamp in the name)."""
    removed = []
    for f in sorted(target_dir.iterdir()):
        m = NAME_RE.match(f.name)
        if m and now - datetime.strptime(m.group(1), _STAMP).replace(tzinfo=UTC) > timedelta(
            days=retention_days
        ):
            f.unlink()
            removed.append(f)
    return removed


def _extract(archive: Path, dest: Path) -> None:
    """Write only regular files and directories whose names stay inside `dest`."""
    with tarfile.open(archive, "r") as tf:
        for m in tf.getmembers():
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                raise NiveshError(f"unsafe member in backup archive: {m.name!r}")
            out = dest.joinpath(*p.parts)
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                raise NiveshError(f"unreadable member in backup archive: {m.name!r}")
            with open(out, "wb") as f:
                shutil.copyfileobj(src, f)


def restore_backup(archive: Path, data_dir: Path, identity: Path) -> None:
    with private_umask(), tempfile.TemporaryDirectory() as tmp:
        plain, stage = Path(tmp) / "backup.tar", Path(tmp) / "stage"
        stage.mkdir()
        if any((data_dir / n).exists() for n in (*_DB_FILES, "runs")):
            raise NiveshError(f"{data_dir} already holds a Nivesh database; restore to a fresh dir")
        _decrypt(archive, plain, identity)
        try:
            _extract(plain, stage)
        except (tarfile.TarError, OSError, EOFError) as e:
            raise NiveshError(f"backup archive is corrupt or not a Nivesh backup: {e}") from None
        if not (stage / _DB_FILES[0]).exists():
            raise NiveshError("archive does not contain a Nivesh database")
        created = not data_dir.exists()
        ensure_data_dir(data_dir)
        placed: list[Path] = []
        try:  # nivesh.sqlite last: its presence is what marks the dir as restored
            for item in sorted(stage.iterdir(), key=lambda p: p.name == _DB_FILES[0]):
                shutil.move(str(item), str(data_dir / item.name))
                placed.append(data_dir / item.name)
        except BaseException:
            for p in placed:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            if created:
                shutil.rmtree(data_dir, ignore_errors=True)
            raise
