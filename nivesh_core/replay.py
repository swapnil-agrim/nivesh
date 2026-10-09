"""Deterministic replay of recorded engine calls (ST-13.3 AC2, plumbing only).

Engines register pure functions in REPLAYABLE (empty until E6). A replay recomputes every
`engine_call` in a trace and requires the result to be exactly equal (no tolerance).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nivesh_core.trace import read_trace

REPLAYABLE: dict[str, Callable[..., Any]] = {}


@dataclass
class Replay:
    steps: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def replay_run(run_dir: Path) -> Replay:
    out = Replay()
    for rec in read_trace(run_dir / "trace.jsonl"):
        if rec["type"] != "engine_call":
            continue
        out.steps += 1
        name, step = rec["engine"], rec["seq"]
        fn = REPLAYABLE.get(name)
        if fn is None:
            out.failures.append(f"step {step}: unreplayable engine {name!r}")
            continue
        args = rec.get("args", [])
        got = fn(**args) if isinstance(args, dict) else fn(*args)
        if _canon(got) != _canon(rec.get("result")):
            out.failures.append(f"step {step}: {name} result differs from the recorded one")
    return out
