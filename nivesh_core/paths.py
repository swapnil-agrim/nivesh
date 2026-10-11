import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from nivesh_core.errors import ConfigError

MARKER = ".nivesh"


@contextmanager
def private_umask() -> Iterator[None]:
    """Files created inside are owner-only from birth (no chmod-after window).

    ponytail: umask is process-wide; fine for a single-threaded CLI.
    """
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def ensure_data_dir(path: Path) -> Path:
    """Create or adopt `path` as an owner-only (0700) data dir.

    A non-empty directory without our marker is refused untouched, so a typo like
    `--data-dir ~` can never chmod a user's home.
    """
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"{path}: not a directory")
        if any(path.iterdir()) and not (path / MARKER).exists():
            raise ConfigError(f"{path}: not empty and not a Nivesh data directory; refusing")
    else:
        old = os.umask(0o077)
        try:
            path.mkdir(mode=0o700, parents=True)
        finally:
            os.umask(old)
    os.chmod(path, 0o700)
    with private_umask():
        (path / MARKER).touch()
    return path


def write_private(path: Path, data: str | bytes, *, append: bool = False) -> None:
    """0600-from-birth write; refuses symlinks, and existing files unless appending."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_APPEND if append else os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "ab") as f:
        f.write(data.encode() if isinstance(data, str) else data)


def replace_private(path: Path, data: str | bytes) -> None:
    """Atomically replace `path` with a 0600 file (daily token rotation); refuses a symlink."""
    if path.is_symlink():
        raise ConfigError(f"{path}: is a symlink; refusing to replace")
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    write_private(tmp, data)
    os.replace(tmp, path)


def run_dir(data_dir: Path, run_id: int, day: date | None = None) -> Path:
    """`<data_dir>/runs/<day>/<run_id>` (owner-only), or the legacy `runs/<run_id>` without a
    day (ST-1.7 gap, ST-10.1)."""
    base = data_dir / "runs"
    d = (base / day.isoformat() if day else base) / str(run_id)
    with private_umask():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


_DAY_DIR = re.compile(r"\d{4}-\d\d-\d\d")


def find_run_dir(data_dir: Path, run_id: int) -> Path | None:
    """The existing run directory: the dated one first, then the legacy shape, else None. The
    path is built from the integer only, so a name can never walk out of `runs/`."""
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ConfigError("run number must be a positive integer")
    base = data_dir / "runs"
    if not base.is_dir():
        return None
    for day in sorted((d for d in base.iterdir() if _DAY_DIR.fullmatch(d.name)), reverse=True):
        if (day / str(run_id)).is_dir():
            return day / str(run_id)
    legacy = base / str(run_id)
    return legacy if legacy.is_dir() else None
