"""Stores of the scheduler (ST-11.1) and the alerts (ST-11.4): job claims, fired alerts and the
owner's invalidation levels. SQLite only; every time is aware UTC in and out, stored as ISO text.

`job_run` is mutable: a row is claimed as running and then finished. `alert` and
`invalidation_level` are append-only (the database refuses changes, migration 0009).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nivesh_core.errors import NiveshError
from nivesh_core.timeutil import to_iso

Status = Literal["running", "ok", "failed", "skipped"]


@dataclass(frozen=True)
class JobRow:
    job: str
    trading_date: date
    status: Status
    attempts: int
    run_id: int | None
    started_at: datetime
    finished_at: datetime | None
    reason: str
    late: bool
    notified: bool


_JOB_COLS = (
    "job, trading_date, status, attempts, run_id, started_at, finished_at, reason, late, notified"
)


def claim_job(conn: sqlite3.Connection, job: str, day: date, now: datetime, stale_min: int) -> bool:
    """Take the (job, trading date) key. True when this caller owns it: a new row, or a running
    row older than `stale_min` minutes (a crashed run), which counts as one more attempt. The
    claim is committed before returning, so nothing is held while the job runs."""
    stamp = to_iso(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, started_at FROM job_run WHERE job = ? AND trading_date = ?",
            (job, day.isoformat()),
        ).fetchone()
        won = False
        if row is None:
            conn.execute(
                "INSERT INTO job_run (job, trading_date, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (job, day.isoformat(), stamp),
            )
            won = True
        elif row[0] == "running" and datetime.fromisoformat(row[1]) <= now - timedelta(
            minutes=stale_min
        ):
            conn.execute(
                "UPDATE job_run SET started_at = ?, attempts = attempts + 1 "
                "WHERE job = ? AND trading_date = ?",
                (stamp, job, day.isoformat()),
            )
            won = True
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return won


def release_job(conn: sqlite3.Connection, job: str, day: date) -> None:
    """Give a claim back so a later tick can try again (only a row still running)."""
    conn.execute(
        "DELETE FROM job_run WHERE job = ? AND trading_date = ? AND status = 'running'",
        (job, day.isoformat()),
    )


def finish_job(
    conn: sqlite3.Connection, job: str, day: date, status: Status, attempts: int,
    run_id: int | None, reason: str, late: bool, notified: bool, now: datetime,
) -> None:  # fmt: skip
    """Close a claimed row; `attempts` made now are added to those of an earlier crashed run."""
    conn.execute(
        "UPDATE job_run SET status = ?, attempts = attempts + ?, run_id = ?, finished_at = ?, "
        "reason = ?, late = ?, notified = ? WHERE job = ? AND trading_date = ?",
        (status, attempts, run_id, to_iso(now), reason, int(late), int(notified), job,
         day.isoformat()),
    )  # fmt: skip


def finished_jobs(conn: sqlite3.Connection, since: date) -> set[tuple[str, date]]:
    """(job, trading date) pairs finished on or after `since`. A running row is left out on
    purpose: `claim_job` decides whether it is still alive or a crash to reclaim."""
    rows = conn.execute(
        "SELECT job, trading_date FROM job_run WHERE trading_date >= ? AND status != 'running'",
        (since.isoformat(),),
    ).fetchall()
    return {(str(r[0]), date.fromisoformat(r[1])) for r in rows}


def _job(r: tuple[Any, ...]) -> JobRow:
    return JobRow(
        r[0], date.fromisoformat(r[1]), r[2], int(r[3]), r[4], datetime.fromisoformat(r[5]),
        None if r[6] is None else datetime.fromisoformat(r[6]), r[7] or "", bool(r[8]), bool(r[9]),
    )  # fmt: skip


def job_rows(conn: sqlite3.Connection) -> list[JobRow]:
    """The newest row of every job."""
    rows = conn.execute(
        f"SELECT {_JOB_COLS} FROM job_run j WHERE trading_date = "  # noqa: S608
        "(SELECT max(trading_date) FROM job_run WHERE job = j.job) ORDER BY job"
    ).fetchall()
    return [_job(r) for r in rows]


# ---- alerts -------------------------------------------------------------------------------------
class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alert_id: int | None = None
    rule: str = Field(min_length=1)
    security_id: int | None = None
    fired_at: datetime
    trading_date: date
    payload: dict[str, Any]
    delivered: Literal[0, 1, 2] = 0
    digest_id: int | None = None


_ALERT_COLS = "alert_id, rule, security_id, fired_at, trading_date, payload, delivered, digest_id"


def record_alert(conn: sqlite3.Connection, a: Alert) -> int:
    """Append one alert; the row never changes afterwards."""
    cur = conn.execute(
        "INSERT INTO alert (rule, security_id, fired_at, trading_date, payload, delivered, "
        "digest_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (a.rule, a.security_id, to_iso(a.fired_at), a.trading_date.isoformat(),
         json.dumps(a.payload, sort_keys=True), a.delivered, a.digest_id),
    )  # fmt: skip
    return int(cur.lastrowid or 0)


def _alert(r: tuple[Any, ...]) -> Alert:
    return Alert(
        alert_id=r[0], rule=r[1], security_id=r[2], fired_at=datetime.fromisoformat(r[3]),
        trading_date=date.fromisoformat(r[4]), payload=json.loads(r[5]), delivered=r[6],
        digest_id=r[7],
    )  # fmt: skip


def recent_alerts(
    conn: sqlite3.Connection, rule: str, security_id: int | None, since: date
) -> list[Alert]:
    """Alerts of one rule and security that were sent (alone or in a digest) on or after `since`,
    oldest first: the history that decides whether a new one is a repeat."""
    rows = conn.execute(
        f"SELECT {_ALERT_COLS} FROM alert WHERE rule = ? AND security_id IS ? "  # noqa: S608
        "AND trading_date >= ? AND delivered > 0 ORDER BY alert_id",
        (rule, security_id, since.isoformat()),
    ).fetchall()
    return [_alert(r) for r in rows]


def alerts_since(conn: sqlite3.Connection, since: datetime, rule: str | None = None) -> list[Alert]:
    """Alerts fired at or after the aware instant `since`, oldest first."""
    sql = f"SELECT {_ALERT_COLS} FROM alert WHERE fired_at >= ?"  # noqa: S608
    args: list[Any] = [to_iso(since)]
    if rule is not None:
        sql += " AND rule = ?"
        args.append(rule)
    return [_alert(r) for r in conn.execute(sql + " ORDER BY alert_id", args).fetchall()]


def sent_alerts(conn: sqlite3.Connection, since: date) -> list[Alert]:
    """Alerts that went out (alone or in a digest) with a trading date on or after `since`."""
    rows = conn.execute(
        f"SELECT {_ALERT_COLS} FROM alert WHERE trading_date >= ? AND delivered > 0 "  # noqa: S608
        "ORDER BY alert_id",
        (since.isoformat(),),
    ).fetchall()
    return [_alert(r) for r in rows]


def count_alerts(
    conn: sqlite3.Connection, start: datetime, end: datetime, like: str | None = None
) -> int:
    """Alerts fired in [start, end) whose payload text matches the SQL LIKE pattern `like`."""
    sql = "SELECT count(*) FROM alert WHERE fired_at >= ? AND fired_at < ?"
    args: list[Any] = [to_iso(start), to_iso(end)]
    if like is not None:
        sql += " AND payload LIKE ?"
        args.append(like)
    return int(conn.execute(sql, args).fetchone()[0])


def has_alert_for(
    conn: sqlite3.Connection, rule: str, security_id: int, key: str, value: str
) -> bool:
    """Whether an alert of this rule and security already carries `key: value` in its payload,
    sent or not (so a change in fund data is reported once)."""
    like = f'%"{key}": "{value}"%'
    row = conn.execute(
        "SELECT 1 FROM alert WHERE rule = ? AND security_id = ? AND payload LIKE ? LIMIT 1",
        (rule, security_id, like),
    ).fetchone()
    return row is not None


def next_digest_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT max(digest_id) FROM alert").fetchone()
    return int(row[0] or 0) + 1


# ---- invalidation levels ------------------------------------------------------------------------
def set_invalidation(
    conn: sqlite3.Connection, security_id: int, level: Decimal, set_on: date, note: str | None
) -> None:
    if not level.is_finite() or level <= 0:
        raise NiveshError("the invalidation level must be a positive number")
    conn.execute(
        "INSERT INTO invalidation_level (security_id, level, set_on, note) VALUES (?, ?, ?, ?)",
        (security_id, format(level, "f"), set_on.isoformat(), note),
    )


def invalidation_levels(conn: sqlite3.Connection) -> dict[int, Decimal]:
    """The latest level per security."""
    rows = conn.execute("SELECT security_id, level FROM invalidation_level ORDER BY id").fetchall()
    return {int(r[0]): Decimal(r[1]) for r in rows}  # later rows overwrite earlier ones
