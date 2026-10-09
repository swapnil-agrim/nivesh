"""Per-run JSONL trace (ST-13.3). One record per line, appended and flushed immediately so a crash
leaves a partial but valid trace. Payload fields are redacted (NFR-3); metadata is not, because
key-name masking would hit names like `input_tokens`.
"""

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nivesh_core.paths import write_private
from nivesh_core.redact import redact_json
from nivesh_core.timeutil import to_iso, utcnow


def prompt_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class Tracer:
    def __init__(
        self, run_dir: Path, run_id: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.path = run_dir / "trace.jsonl"
        self.run_id = run_id
        self._clock = clock
        self._t0 = clock()
        self._seq = 0
        self.prompt_ver: str | None = None
        self.summary: dict[str, Any] = {}  # last `result` record, for the run row

    def _emit(
        self, type_: str, meta: dict[str, Any], payload: dict[str, Any] | None = None
    ) -> None:
        self._seq += 1
        rec = {"seq": self._seq, "ts": to_iso(utcnow()), "type": type_, **meta}
        rec.update(redact_json(payload or {}))
        write_private(self.path, json.dumps(rec, default=str) + "\n", append=True)

    def start(self, command: str, prompt_ver: str, model: str | None = None) -> None:
        self.prompt_ver = prompt_ver
        self._emit("start", {"run_id": self.run_id, "prompt_version": prompt_ver, "model": model},
                   {"command": command})  # fmt: skip

    def assistant_text(self, text: str) -> None:
        self._emit("assistant_text", {}, {"text": text})

    def tool_call(self, tool_id: str, name: str, args: Any) -> None:
        self._emit("tool_call", {"tool_id": tool_id, "tool": name}, {"args": args})

    def tool_result(self, tool_id: str, output: Any, is_error: bool = False) -> None:
        self._emit(
            "tool_result", {"tool_id": tool_id, "is_error": bool(is_error)}, {"output": output}
        )

    def engine_call(self, name: str, args: Any, result: Any) -> None:
        self._emit("engine_call", {"engine": name}, {"args": args, "result": result})

    def error(self, message: str) -> None:
        self._emit("error", {}, {"message": message})

    def result(
        self,
        *,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_inr: float,
        cost_source: str,
    ) -> None:
        self.summary = {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round((self._clock() - self._t0) * 1000),
            "cost_inr": cost_inr,
            "cost_source": cost_source,
        }
        self._emit("result", self.summary)


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
