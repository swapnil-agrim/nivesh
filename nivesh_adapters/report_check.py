"""Check a report's numbers against the run that produced it (ST-10.6).

Evidence comes from three sources: tool results in the trace, the committee snapshot (engine
outputs, unredacted), and the facts the code gave `save_report` (`report_input.json`). The
report's own registered numbers and dates join the pool, but its verdict, committee views and
rendered blocks never do. A miss stamps the DRAFT banner and lowers the run to `needs_review`.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from nivesh_adapters.report import BANNER, Report
from nivesh_core.trace import read_trace
from nivesh_engine.citations import ALL, Evidence, Validation, validate

NEEDS_REVIEW = "needs_review"
_SKIPPED_SNAPSHOT = ("securities", "run_id", "prompt_versions", "models", "config_digest")


class HasStatus(Protocol):
    status: str


def _facts(evidence: Evidence, facts: dict[str, Any]) -> None:
    by_security = facts.get("by_security")
    for key, value in facts.items():
        if key == "by_security" and isinstance(by_security, dict):
            for sid, part in by_security.items():
                evidence.add_json(part, str(sid))
        else:
            evidence.add_json(value, ALL)


def _snapshot(evidence: Evidence, run_dir: Path) -> None:
    path = run_dir / "snapshot.json"
    if not path.is_file():
        return
    snap = json.loads(path.read_text())
    evidence.add_json(snap.get("as_of"), ALL)
    evidence.add_json(snap.get("profile_limits"), ALL)
    for part in ("engine_outputs", "coverage"):
        for sid, data in (snap.get(part) or {}).items():
            evidence.add_json(data, str(sid))


def evidence_from_run(run_dir: Path, report: Report, facts: dict[str, Any]) -> Evidence:
    """The pool a report is held to. Redacted trace text does not count as evidence."""
    ev = Evidence()
    trace = run_dir / "trace.jsonl"
    if trace.is_file():
        for rec in read_trace(trace):
            if rec.get("type") == "tool_result" and not rec.get("is_error"):
                sid = rec.get("security_id")
                ev.add_json(rec.get("output"), ALL if sid is None else str(sid))
    _snapshot(ev, run_dir)
    _facts(ev, facts)
    for n in report.nums:
        ev.add_registered(n.value, n.decimals, n.unit)
    for d in (*report.as_of, *report.dates):
        ev.add_day(d)
    return ev


def _lines(v: Validation) -> tuple[str, ...]:
    return tuple(f"{m.figure} in {m.block_id}: {m.context} ({m.reason})" for m in v.unmatched)


def apply_validation(report: Report, validation: Validation) -> Report:
    """The report, stamped DRAFT with each unmatched number when there is one."""
    if validation.clean:
        return report
    return replace(report, banner=BANNER, unmatched=_lines(validation))


def finalize(
    report: Report, facts: dict[str, Any], run_dir: Path, *, max_digits: int = 6,
    metered: HasStatus | None = None,
) -> Report:  # fmt: skip
    """Validate, stamp and lower the run status. An "ok" run becomes `needs_review` on any miss;
    an error stays an error."""
    ev = evidence_from_run(run_dir, report, facts)
    result = validate(report.checked_texts(), ev, max_digits)
    if not result.clean and metered is not None and metered.status == "ok":
        metered.status = NEEDS_REVIEW
    return apply_validation(report, result)
