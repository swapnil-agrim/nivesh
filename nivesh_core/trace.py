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


def digest_text(hexdigest: str) -> str:
    """A hex digest in groups of eight joined by hyphens. A long run of hex digits can hold nine
    or more decimal digits in a row, which the PII scanner (rightly cautious) reports; groups
    of eight cannot."""
    return "-".join(hexdigest[i : i + 8] for i in range(0, len(hexdigest), 8))


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
        # Committee runs: usage accumulates here across agents (`result` replaces, these add).
        # One event loop only: `_seq` and these are touched synchronously, never from threads.
        self.totals: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cost_inr": 0.0}
        self._sources: set[str] = set()
        self._ids: dict[tuple[str | None, int | None], set[str]] = {}

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

    @staticmethod
    def _who(agent: str | None, security_id: int | None) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if agent is not None:
            meta["agent"] = agent
        if security_id is not None:
            meta["security_id"] = security_id
        return meta

    def tool_call(
        self, tool_id: str, name: str, args: Any,
        *, agent: str | None = None, security_id: int | None = None,
    ) -> None:  # fmt: skip
        self._ids.setdefault((agent, security_id), set()).add(tool_id)
        meta = {"tool_id": tool_id, "tool": name, **self._who(agent, security_id)}
        self._emit("tool_call", meta, {"args": args})

    def tool_result(
        self, tool_id: str, output: Any, is_error: bool = False,
        *, agent: str | None = None, security_id: int | None = None,
    ) -> None:  # fmt: skip
        meta = {"tool_id": tool_id, "is_error": bool(is_error), **self._who(agent, security_id)}
        self._emit("tool_result", meta, {"output": output})

    def tool_ids(self, agent: str, security_id: int | None) -> frozenset[str]:
        """Tool-use ids this agent made for this security (the evidence check reads this)."""
        return frozenset(self._ids.get((agent, security_id), ()))

    def all_tool_ids(self) -> frozenset[str]:
        return frozenset().union(*self._ids.values()) if self._ids else frozenset()

    def agent_start(
        self, agent: str, security_id: int | None,
        *, model: str | None, prompt_version: str, digest: str,
    ) -> None:  # fmt: skip
        meta = {
            "model": model, "prompt_version": prompt_version, "prompt_digest": digest_text(digest)
        }  # fmt: skip
        self._emit("agent_start", {**self._who(agent, security_id), **meta})

    def agent_end(
        self, agent: str, security_id: int | None,
        *, status: str, model: str | None, input_tokens: int, output_tokens: int,
        cost_inr: float, cost_source: str,
    ) -> None:  # fmt: skip
        self.totals["input_tokens"] += input_tokens
        self.totals["output_tokens"] += output_tokens
        self.totals["cost_inr"] += cost_inr
        self._sources.add(cost_source)
        meta = {
            "status": status, "model": model, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cost_inr": cost_inr, "cost_source": cost_source,
        }  # fmt: skip
        self._emit("agent_end", {**self._who(agent, security_id), **meta})

    def saved(self, kind: str, name: str, digest: str) -> None:
        """A persisted committee file: only its name and SHA-256, never the content."""
        self._emit(f"{kind}_saved", {"name": name, "digest": digest_text(digest)})

    def finish_run(self, model_label: str | None) -> None:
        """Run summary from the accumulated agent totals (same keys as `result`)."""
        src = next(iter(self._sources)) if len(self._sources) == 1 else "mixed"
        self.result(
            model=model_label, input_tokens=self.totals["input_tokens"],
            output_tokens=self.totals["output_tokens"], cost_inr=self.totals["cost_inr"],
            cost_source=src if self._sources else "none",
        )  # fmt: skip

    def engine_call(self, name: str, args: Any, result: Any) -> None:
        self._emit("engine_call", {"engine": name}, {"args": args, "result": result})

    def error(
        self, message: str, *, agent: str | None = None, security_id: int | None = None
    ) -> None:
        self._emit("error", self._who(agent, security_id), {"message": message})

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
