"""PII scanner (ST-13.2): reports kinds and positions, never the matched text."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nivesh_core.redact import PATTERNS


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def scan_text(text: str) -> list[Finding]:
    return sorted(
        (Finding(k, m.start(), m.end()) for k, p in PATTERNS.items() for m in p.finditer(text)),
        key=lambda f: (f.start, f.kind),
    )


def scan_paths(paths: Iterable[Path]) -> list[tuple[Path, int, str]]:
    """(file, 1-based line, kind) for every finding in text files under `paths`."""
    out: list[tuple[Path, int, str]] = []
    for root in paths:
        for f in sorted(root.rglob("*") if root.is_dir() else [root]):
            if not f.is_file():
                continue
            try:
                raw = f.read_bytes()
                lines = raw.decode().splitlines() if b"\0" not in raw else []
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable: skipped
            out += [(f, n, x.kind) for n, ln in enumerate(lines, 1) for x in scan_text(ln)]
    return out
