import os
from collections.abc import Iterator
from contextlib import contextmanager
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
