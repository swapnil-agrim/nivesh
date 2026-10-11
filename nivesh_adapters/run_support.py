"""The run lifecycle shared by commands and the scheduler: a run row, a dated directory and a
trace around work, with the budget gate and the egress check for work that spends on a model.

No terminal code lives here. Callers pass `clock` (so run rows and directories follow an
injected instant) and `warn` (how a warning is shown). A refused budget raises `BudgetRefused`
before any run row is written.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from nivesh_core.config import Settings
from nivesh_core.cost import gate, month_to_date
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.egress import check_egress
from nivesh_core.errors import NiveshError, SecretNotFound
from nivesh_core.paths import run_dir
from nivesh_core.profile import Profile
from nivesh_core.secrets import SecretRef
from nivesh_core.timeutil import ist_date, to_iso, utcnow
from nivesh_core.trace import Tracer

Clock = Callable[[], datetime]
Warn = Callable[[str], None]


class BudgetRefused(NiveshError):
    """The monthly cost cap does not allow this run; the message says why."""


@dataclass
class Metered:
    """One run: its row id, trace, directory and SDK env. Set `status` to "ok" when the work
    succeeds; the row is finished as "error" otherwise."""

    run_id: int
    tier: str
    tracer: Tracer
    run_dir: Path
    env: dict[str, str] = field(default_factory=dict)
    status: str = "error"


@contextmanager
def metered_run(
    settings: Settings, profile: Profile, command: str, tier: str, *, force: bool,
    clock: Clock = utcnow, warn: Warn | None = None, strict: bool = False,
) -> Iterator[Metered]:  # fmt: skip
    """SDK environment, egress check, budget gate, run row and tracer around one model-spending
    run. `strict` (unattended work) also refuses a run once the cap is reached, whatever the
    tier, because nobody is there to read the warning."""
    env: dict[str, str] = {}
    if settings.anthropic_api_key:
        try:
            env["ANTHROPIC_API_KEY"] = SecretRef(settings.anthropic_api_key).resolve()
        except SecretNotFound:
            pass  # fall back to an existing Claude Code login / environment
    if settings.registered_ip:  # CR-2: report, never retry or block
        res = check_egress(settings.registered_ip, settings.egress_url)
        if res.status in ("mismatch", "error") and warn:
            warn(res.message)
    data_dir = Path(settings.data_dir)
    init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        at = clock()
        spent = month_to_date(conn, at)
        decision = gate(spent, profile.monthly_cost_cap, tier, force)
        if not decision.allowed:
            raise BudgetRefused(decision.message)
        if strict and profile.monthly_cost_cap and spent >= profile.monthly_cost_cap:
            raise BudgetRefused(f"{decision.message}: unattended runs stop at the cap")
        if decision.warn and warn:
            warn(decision.message)
        day = ist_date(at)
        run_id = _start_run(conn, command, decision.tier, day, at)
        rdir = run_dir(data_dir, run_id, day)
        m = Metered(run_id, decision.tier, Tracer(rdir, run_id), rdir, env)
        try:
            yield m
        finally:
            _finish_run(conn, run_id, m.status, m.tracer, clock())
    finally:
        conn.close()


@contextmanager
def plain_run(settings: Settings, command: str, *, clock: Clock = utcnow) -> Iterator[Metered]:
    """A run row, dated directory and trace for work that spends nothing: no budget gate, no
    egress check, no model access. Set `status` to "ok" when the work succeeds."""
    data_dir = Path(settings.data_dir)
    init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        at = clock()
        day = ist_date(at)
        run_id = _start_run(conn, command, "none", day, at)
        rdir = run_dir(data_dir, run_id, day)
        m = Metered(run_id, "none", Tracer(rdir, run_id), rdir)
        try:
            yield m
        finally:
            _finish_run(conn, run_id, m.status, m.tracer, clock())
    finally:
        conn.close()


def _start_run(conn: sqlite3.Connection, command: str, tier: str, day: date, at: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO run (command, started_at, status, tier) VALUES (?, ?, 'running', ?)",
        (command, to_iso(at), tier),
    )
    run_id = int(cur.lastrowid or 0)
    conn.execute("UPDATE run SET run_dir = ? WHERE id = ?", (f"runs/{day}/{run_id}", run_id))
    return run_id


def _finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, tracer: Tracer, at: datetime
) -> None:
    r = tracer.summary
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, model = ?, prompt_version = ?, "
        "input_tokens = ?, output_tokens = ?, cost_inr = ? WHERE id = ?",
        (
            to_iso(at), status, r.get("model"), tracer.prompt_ver,
            r.get("input_tokens", 0), r.get("output_tokens", 0), r.get("cost_inr", 0.0), run_id,
        ),
    )  # fmt: skip
