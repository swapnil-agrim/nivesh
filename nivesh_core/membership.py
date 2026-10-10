"""Index membership from an owner-supplied constituent file (ST-9.1). Nothing is fetched: the
CSV has a `symbol` column and optional `isin` and `sector` columns, and each row is resolved to a
`security_id` through the security master (ISIN first, then symbol inside the index's market).
Rows that do not resolve are reported with their symbols, never dropped silently.
"""

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from nivesh_core.errors import NiveshError
from nivesh_core.security_master import SecurityMaster


@dataclass(frozen=True)
class MemberRow:
    symbol: str
    isin: str | None = None
    sector: str | None = None


@dataclass
class LoadReport:
    index_id: str
    total: int
    loaded: int
    unresolved: list[str] = field(default_factory=list)


def parse_constituents(text: str) -> list[MemberRow]:
    """Rows of a constituent CSV; a missing `symbol` column or an empty file is an error."""
    if not text.strip():
        raise NiveshError("the constituent file is empty")
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    if "symbol" not in fields:
        raise NiveshError("the constituent file needs a `symbol` column (optional: isin, sector)")
    out: list[MemberRow] = []
    for raw in reader:
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not row.get("symbol"):
            continue
        out.append(MemberRow(row["symbol"], row.get("isin") or None, row.get("sector") or None))
    if not out:
        raise NiveshError("the constituent file has no rows")
    return out


def read_constituent_file(path: Path) -> list[MemberRow]:
    try:
        text = path.read_text()
    except OSError as e:
        raise NiveshError(f"cannot read {path}: {e.strerror}") from None
    return parse_constituents(text)


def _resolve(master: SecurityMaster, row: MemberRow, market: str) -> int | None:
    if row.isin:
        hit = [s for s in master.by_isin(row.isin) if s.market == market]
        if hit:
            return hit[0].id
    named = master.by_symbol(row.symbol, market)
    return named[0].id if named else None


def load_members(
    conn: sqlite3.Connection, master: SecurityMaster, index_id: str, market: str,
    rows: list[MemberRow], as_of: date,
) -> LoadReport:  # fmt: skip
    """Replace the snapshot of `index_id` with the resolved rows, in one transaction. A file
    where nothing resolves is refused and the old snapshot stays."""
    resolved: dict[int, MemberRow] = {}
    unresolved: list[str] = []
    for row in rows:
        sid = _resolve(master, row, market)
        if sid is None:
            unresolved.append(row.symbol)
        else:
            resolved.setdefault(sid, row)
    if not resolved:
        raise NiveshError(
            f"none of the {len(rows)} rows resolved to a known security in {market}; "
            "run `nivesh master build` first"
        )
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM index_member WHERE index_id = ?", (index_id,))
        for sid, row in sorted(resolved.items()):
            conn.execute(
                "INSERT INTO index_member (index_id, security_id, as_of) VALUES (?, ?, ?)",
                (index_id, sid, as_of.isoformat()),
            )
            if row.sector:  # the file fills a missing sector; master data is not overruled
                conn.execute(
                    "UPDATE security SET sector = ? WHERE id = ? AND sector IS NULL",
                    (row.sector, sid),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return LoadReport(index_id, len(rows), len(resolved), unresolved)


def members_of(conn: sqlite3.Connection, index_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT security_id FROM index_member WHERE index_id = ? ORDER BY security_id",
        (index_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def latest_as_of(conn: sqlite3.Connection, index_id: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(as_of) FROM index_member WHERE index_id = ?", (index_id,)
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None
