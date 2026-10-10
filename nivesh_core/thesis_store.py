"""SQLite persistence of theses (migration 0006). Append-only rows; one active row per security."""

import json
import sqlite3
from typing import Any

from pydantic import TypeAdapter

from nivesh_core.errors import NiveshError
from nivesh_core.thesis import KillCriterion, Thesis

_CRITERIA = TypeAdapter(list[KillCriterion])
_COLS = (
    "thesis_id, security_id, created_at, horizon, why, kill_criteria, target_review_date, "
    "status, source"
)


def save_thesis(conn: Any, thesis: Thesis) -> int:
    """Insert a thesis row and return its id; a second active row for a security is refused."""
    try:
        cur = conn.execute(
            "INSERT INTO thesis (security_id, created_at, horizon, why, kill_criteria, "
            "target_review_date, status, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thesis.security_id,
                thesis.created_at.isoformat(),
                thesis.horizon,
                thesis.why,
                _CRITERIA.dump_json(thesis.kill_criteria).decode(),
                thesis.review_date.isoformat(),
                thesis.status,
                thesis.source,
            ),
        )
    except sqlite3.IntegrityError as e:
        if "thesis.security_id" in str(e) and "UNIQUE" in str(e):
            raise NiveshError(f"security {thesis.security_id} already has an active thesis") from e
        raise
    return int(cur.lastrowid)


def _row(r: tuple[Any, ...]) -> Thesis:
    keys = (
        "thesis_id", "security_id", "created_at", "horizon", "why", "kill_criteria",
        "review_date", "status", "source",
    )  # fmt: skip
    data = dict(zip(keys, r, strict=True))
    data["kill_criteria"] = json.loads(data["kill_criteria"])
    return Thesis.model_validate_json(json.dumps(data))


def active_theses(conn: Any) -> dict[int, Thesis]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM thesis WHERE status = 'active' ORDER BY security_id"  # noqa: S608
    ).fetchall()
    return {t.security_id: t for t in map(_row, rows)}


def active_thesis(conn: Any, security_id: int) -> Thesis | None:
    r = conn.execute(
        f"SELECT {_COLS} FROM thesis WHERE status = 'active' AND security_id = ?",  # noqa: S608
        (security_id,),
    ).fetchone()
    return None if r is None else _row(r)
