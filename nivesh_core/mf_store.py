"""DuckDB reads/writes for mutual-fund tables (E5). Writes happen here and in `mf_ingest.py`.

NAV precedence: where several sources hold a date, the best-ranked source wins (MFapi over AMFI).
`fetched_at` / `detected_at` are store-side clock reads, outside the engine's no-clock rule.
"""

from collections.abc import Sequence
from datetime import date, datetime

import duckdb

from nivesh_core.market_store import _in_txn
from nivesh_core.mf_models import FundHoldingRow, FundMeta, NavGap, NavPoint
from nivesh_core.timeutil import to_iso, utcnow

_CHUNK = 400
NAV_SOURCE_PRIORITY = ("mfapi", "amfi_navall")


def _rank(source: str) -> int:
    n = len(NAV_SOURCE_PRIORITY)
    return NAV_SOURCE_PRIORITY.index(source) if source in NAV_SOURCE_PRIORITY else n


def upsert_nav(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    points: Sequence[NavPoint],
    fetched_at: datetime | None = None,
) -> None:
    """Idempotent per (security, date, source); all rows or none."""
    stamp = to_iso(fetched_at or utcnow())
    rows = [(security_id, p.date, p.nav, p.source, stamp) for p in points]

    def work() -> None:  # multi-row VALUES: far faster than a row at a time for a full history
        for i in range(0, len(rows), _CHUNK):
            part = rows[i : i + _CHUNK]
            marks = ", ".join(["(?, ?, ?, ?, ?)"] * len(part))
            conn.execute(
                f"INSERT OR REPLACE INTO nav_point VALUES {marks}",  # noqa: S608
                [v for r in part for v in r],
            )

    _in_txn(conn, work)


def get_nav(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    start: date | None = None,
    end: date | None = None,
) -> list[NavPoint]:
    """Oldest first, one point per date: the best-ranked source's."""
    rows = conn.execute(
        "SELECT date, nav, source FROM nav_point WHERE security_id = ? "
        "AND date >= COALESCE(?, DATE '0001-01-01') AND date <= COALESCE(?, DATE '9999-12-31')",
        (security_id, start, end),
    ).fetchall()
    best: dict[date, NavPoint] = {}
    for d, nav, source in rows:
        cur = best.get(d)
        if cur is None or _rank(source) < _rank(cur.source):
            best[d] = NavPoint(date=d, nav=nav, source=source)
    return [best[d] for d in sorted(best)]


def nav_by_source(
    conn: duckdb.DuckDBPyConnection, security_id: int
) -> dict[str, dict[date, NavPoint]]:
    out: dict[str, dict[date, NavPoint]] = {}
    for d, nav, source in conn.execute(
        "SELECT date, nav, source FROM nav_point WHERE security_id = ?", (security_id,)
    ).fetchall():
        out.setdefault(source, {})[d] = NavPoint(date=d, nav=nav, source=source)
    return out


def last_nav_date(conn: duckdb.DuckDBPyConnection, security_id: int) -> date | None:
    row = conn.execute(
        "SELECT MAX(date) FROM nav_point WHERE security_id = ?", (security_id,)
    ).fetchone()
    return None if row is None else row[0]


_META_COLS = (
    "as_of, amfi_code, scheme_name, amc, category, plan, option, expense_ratio, aum_crore, "
    "benchmark, manager, manager_since, source"
)


def write_fund_meta(conn: duckdb.DuckDBPyConnection, security_id: int, meta: FundMeta) -> None:
    """Append-only by as_of (the same as_of and source is replaced)."""
    row = (
        security_id, meta.as_of, meta.amfi_code, meta.scheme_name, meta.amc, meta.category,
        meta.plan, meta.option, meta.expense_ratio, meta.aum_crore, meta.benchmark, meta.manager,
        meta.manager_since, meta.source,
    )  # fmt: skip
    sql = "INSERT OR REPLACE INTO fund_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    _in_txn(conn, lambda: conn.execute(sql, row))


def latest_fund_meta(
    conn: duckdb.DuckDBPyConnection, security_id: int, as_of: date | None = None
) -> FundMeta | None:
    """The newest metadata on or before `as_of` (default: newest overall)."""
    row = conn.execute(
        f"SELECT {_META_COLS} FROM fund_meta WHERE security_id = ? "  # noqa: S608
        "AND as_of <= COALESCE(?, DATE '9999-12-31') ORDER BY as_of DESC, source LIMIT 1",
        (security_id, as_of),
    ).fetchone()
    if row is None:
        return None
    keys = [c.strip() for c in _META_COLS.split(",")]
    return FundMeta(**dict(zip(keys, row, strict=True)))


def write_fund_holdings(
    conn: duckdb.DuckDBPyConnection, security_id: int, rows: Sequence[FundHoldingRow]
) -> None:
    """Replace each (month, source) present in `rows` wholesale, so a re-ingest is idempotent and
    a line the source dropped does not linger."""
    data = [
        (security_id, r.month_end, r.isin, r.weight_pct, r.holding_security_id, r.kind, r.source)
        for r in rows
    ]
    parts = sorted({(r.month_end, r.source) for r in rows})

    def work() -> None:
        if not data:
            return
        conn.executemany(
            "DELETE FROM fund_holding WHERE security_id = ? AND month_end = ? AND source = ?",
            [(security_id, m, src) for m, src in parts],
        )
        conn.executemany("INSERT INTO fund_holding VALUES (?, ?, ?, ?, ?, ?, ?)", data)

    _in_txn(conn, work)


def get_fund_holdings(
    conn: duckdb.DuckDBPyConnection, security_id: int, month_end: date
) -> list[FundHoldingRow]:
    rows = conn.execute(
        "SELECT month_end, isin, weight_pct, holding_security_id, kind, source FROM fund_holding "
        "WHERE security_id = ? AND month_end = ? ORDER BY weight_pct DESC, isin",
        (security_id, month_end),
    ).fetchall()
    return [
        FundHoldingRow(
            month_end=r[0], isin=r[1], weight_pct=r[2], holding_security_id=r[3], kind=r[4],
            source=r[5],
        )
        for r in rows
    ]  # fmt: skip


def months_stored(conn: duckdb.DuckDBPyConnection, security_id: int) -> list[date]:
    rows = conn.execute(
        "SELECT DISTINCT month_end FROM fund_holding WHERE security_id = ? ORDER BY month_end",
        (security_id,),
    ).fetchall()
    return [r[0] for r in rows]


def write_gaps(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    gaps: Sequence[NavGap],
    detected_at: datetime | None = None,
) -> None:
    """Replace the scheme's gap list with `gaps` (recomputed each run, so a backfill clears one)."""
    stamp = to_iso(detected_at or utcnow())
    rows = [(security_id, g.gap_start, g.gap_end, g.missing_days, stamp) for g in gaps]

    def work() -> None:
        conn.execute("DELETE FROM nav_gap WHERE security_id = ?", (security_id,))
        if rows:
            conn.executemany("INSERT INTO nav_gap VALUES (?, ?, ?, ?, ?)", rows)

    _in_txn(conn, work)


def get_gaps(conn: duckdb.DuckDBPyConnection, security_id: int) -> list[NavGap]:
    rows = conn.execute(
        "SELECT gap_start, gap_end, missing_days FROM nav_gap WHERE security_id = ? "
        "ORDER BY gap_start",
        (security_id,),
    ).fetchall()
    return [NavGap(gap_start=r[0], gap_end=r[1], missing_days=r[2]) for r in rows]
