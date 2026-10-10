"""Persistence of a committee run (ST-7.9): the input snapshot and every agent output, as files in
the run directory, written before the verdict call. Each file is created owner-only and never
overwritten; its SHA-256 goes to the trace (the trace never holds the content).

File names are built only from a fixed set of agent names, integers and a sequence number, never
from model output. Outputs are written as they are: they are deliberately not passed through
`redact_json`, which would blank the schema field `key_points`.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from nivesh_core.errors import NiveshError
from nivesh_core.paths import write_private
from nivesh_core.trace import Tracer

KINDS = frozenset(
    {"fundamental", "technical", "news", "macro", "mf", "debate", "lenses", "risk", "verdict"}
)


def dump(data: Any) -> bytes:
    return (json.dumps(data, sort_keys=True, indent=2, default=str) + "\n").encode()


class RunStore:
    def __init__(self, run_dir: Path, tracer: Tracer) -> None:
        if not run_dir.is_dir():
            raise NiveshError(f"run directory {run_dir} does not exist")
        self.dir = run_dir
        self.tracer = tracer
        self.outputs = run_dir / "outputs"
        self._seq = 0

    def snapshot(self, data: dict[str, Any]) -> str:
        return self._write(self.dir / "snapshot.json", "snapshot", data)

    def output(self, kind: str, security_id: int | None, data: Any) -> str:
        """Save one agent output (or a `{"status": "failed", ...}` marker); returns its digest."""
        if kind not in KINDS:
            raise NiveshError(f"unknown output kind {kind!r}")
        if security_id is not None and not isinstance(security_id, int):
            raise NiveshError("security_id must be an integer")
        if not self.outputs.exists():
            os.mkdir(self.outputs, 0o700)
        self._seq += 1
        name = f"{self._seq:03d}_{kind}_{'all' if security_id is None else security_id}.json"
        return self._write(self.outputs / name, "output", data)

    def failed(self, kind: str, security_id: int | None, reason: str) -> str:
        return self.output(kind, security_id, {"status": "failed", "reason": reason})

    def _write(self, path: Path, trace_kind: str, data: Any) -> str:
        body = dump(data)
        write_private(path, body)  # O_EXCL: an existing file is never overwritten
        digest = hashlib.sha256(body).hexdigest()
        self.tracer.saved(trace_kind, path.name, digest)
        return digest
