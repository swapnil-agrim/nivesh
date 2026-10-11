import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import typer

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


@contextmanager
def user_errors() -> Iterator[None]:
    """Expected failures become a clean message on stderr and a non-zero exit."""
    try:
        yield
    except NiveshError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


def settings_of(ctx: typer.Context) -> Settings:
    settings: Settings = ctx.obj[0]
    return settings


def config_dir_of(ctx: typer.Context) -> Path:
    found: Path = ctx.meta.get("config_dir", Path("config"))
    return found


def profile_of(ctx: typer.Context) -> Profile:
    profile: Profile = ctx.obj[1]
    return profile


@dataclass
class Metered:
    """One model-spending run: its row id, trace, directory and SDK env. Set `status` to "ok"
    when the work succeeds; the row is finished as "error" otherwise."""

    run_id: int
    tier: str
    tracer: Tracer
    run_dir: Path
    env: dict[str, str] = field(default_factory=dict)
    status: str = "error"


@contextmanager
def metered_run(
    settings: Settings, profile: Profile, command: str, tier: str, *, force: bool
) -> Iterator[Metered]:
    """Credentials, egress check, budget gate, run row and tracer around one model-spending
    command. A refused gate exits 1 before any row is written."""
    env: dict[str, str] = {}
    if settings.anthropic_api_key:
        try:
            env["ANTHROPIC_API_KEY"] = SecretRef(settings.anthropic_api_key).resolve()
        except SecretNotFound:
            pass  # fall back to an existing Claude Code login / environment
    if settings.registered_ip:  # CR-2: report, never retry or block
        res = check_egress(settings.registered_ip, settings.egress_url)
        if res.status in ("mismatch", "error"):
            typer.echo(f"warning: {res.message}", err=True)
    data_dir = Path(settings.data_dir)
    with user_errors():
        init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        decision = gate(month_to_date(conn, utcnow()), profile.monthly_cost_cap, tier, force)
        if decision.warn:
            typer.echo(f"warning: {decision.message}", err=True)
        if not decision.allowed:
            raise typer.Exit(1)
        day = ist_date(utcnow())
        run_id = _start_run(conn, command, decision.tier, day)
        rdir = run_dir(data_dir, run_id, day)
        m = Metered(run_id, decision.tier, Tracer(rdir, run_id), rdir, env)
        try:
            yield m
        finally:
            _finish_run(conn, run_id, m.status, m.tracer)
    finally:
        conn.close()


@contextmanager
def plain_run(settings: Settings, command: str) -> Iterator[Metered]:
    """A run row, dated directory and trace for a command that spends nothing: no budget gate,
    no egress check, no credentials. Set `status` to "ok" when the work succeeds."""
    data_dir = Path(settings.data_dir)
    with user_errors():
        init_stores(data_dir)
    conn = open_sqlite(data_dir / "nivesh.sqlite")
    try:
        day = ist_date(utcnow())
        run_id = _start_run(conn, command, "none", day)
        rdir = run_dir(data_dir, run_id, day)
        m = Metered(run_id, "none", Tracer(rdir, run_id), rdir)
        try:
            yield m
        finally:
            _finish_run(conn, run_id, m.status, m.tracer)
    finally:
        conn.close()


def _start_run(conn: sqlite3.Connection, command: str, tier: str, day: date) -> int:
    cur = conn.execute(
        "INSERT INTO run (command, started_at, status, tier) VALUES (?, ?, 'running', ?)",
        (command, to_iso(utcnow()), tier),
    )
    run_id = int(cur.lastrowid or 0)
    conn.execute("UPDATE run SET run_dir = ? WHERE id = ?", (f"runs/{day}/{run_id}", run_id))
    return run_id


def _finish_run(conn: sqlite3.Connection, run_id: int, status: str, tracer: Tracer) -> None:
    r = tracer.summary
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, model = ?, prompt_version = ?, "
        "input_tokens = ?, output_tokens = ?, cost_inr = ? WHERE id = ?",
        (
            to_iso(utcnow()), status, r.get("model"), tracer.prompt_ver,
            r.get("input_tokens", 0), r.get("output_tokens", 0), r.get("cost_inr", 0.0), run_id,
        ),
    )  # fmt: skip
