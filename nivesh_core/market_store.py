"""DuckDB reads/writes for market tables. Writes happen only here and in `market_ingest.py`.

Several sources may hold a bar for one day; reads pick the best-ranked source per date.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import duckdb

from nivesh_core.market_models import CorpAction, PriceBar, ShareholdingRow
from nivesh_core.timeutil import to_iso, utcnow
from nivesh_engine.dedupe import canonical_url
from nivesh_engine.statements import StatementRow

SOURCE_PRIORITY = (
    "nse_bhavcopy",
    "bse_bhavcopy",
    "nse_indices",
    "nse_corp_actions",
    "yahoo",
    "stooq",
)


def _rank(source: str) -> int:
    return SOURCE_PRIORITY.index(source) if source in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


_BAR_SQL = "INSERT OR REPLACE INTO price_bar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
_ACT_SQL = "INSERT OR REPLACE INTO corp_action VALUES (?, ?, ?, ?, ?, ?)"


def _bar_rows(bars: Sequence[PriceBar], fetched_at: datetime | None) -> list[tuple[object, ...]]:
    stamp = to_iso(fetched_at or utcnow())
    return [
        (b.security_id, b.date, b.open, b.high, b.low, b.close, b.volume, b.adj_close, b.source,
         b.flag, stamp)
        for b in bars
    ]  # fmt: skip


def _act_rows(actions: Sequence[CorpAction]) -> list[tuple[object, ...]]:
    return [(a.security_id, a.ex_date, a.kind, a.ratio, a.amount, a.source) for a in actions]


def upsert_bars(
    conn: duckdb.DuckDBPyConnection, bars: Sequence[PriceBar], fetched_at: datetime | None = None
) -> None:
    """Idempotent per (security, date, source); all rows or none."""
    rows = _bar_rows(bars, fetched_at)
    _in_txn(conn, lambda: conn.executemany(_BAR_SQL, rows) if rows else None)


def write_prices(
    conn: duckdb.DuckDBPyConnection,
    bars: Sequence[PriceBar],
    actions: Sequence[CorpAction],
    fetched_at: datetime | None = None,
) -> None:
    """Bars and corporate actions in one transaction (an ingest run lands whole or not at all)."""
    b_rows, a_rows = _bar_rows(bars, fetched_at), _act_rows(actions)

    def work() -> None:
        if a_rows:
            conn.executemany(_ACT_SQL, a_rows)
        if b_rows:
            conn.executemany(_BAR_SQL, b_rows)

    _in_txn(conn, work)


def _in_txn(conn: duckdb.DuckDBPyConnection, work: Callable[[], object]) -> None:
    conn.execute("BEGIN")
    try:
        work()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _bar(r: tuple[object, ...]) -> PriceBar:
    return PriceBar(
        security_id=r[0], date=r[1], open=r[2], high=r[3], low=r[4], close=r[5], volume=r[6],  # type: ignore[arg-type]
        adj_close=r[7], source=r[8], flag=r[9],  # type: ignore[arg-type]
    )  # fmt: skip


_BAR_COLS = "security_id, date, open, high, low, close, volume, adj_close, source, flag"


def bars_by_source(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, list[PriceBar]]:
    rows = conn.execute(
        f"SELECT {_BAR_COLS} FROM price_bar WHERE security_id = ? "  # noqa: S608
        "AND date >= COALESCE(?, DATE '0001-01-01') AND date <= COALESCE(?, DATE '9999-12-31') "
        "ORDER BY date",
        (security_id, start, end),
    ).fetchall()
    out: dict[str, list[PriceBar]] = {}
    for r in rows:
        b = _bar(r)
        out.setdefault(b.source, []).append(b)
    return out


def get_bars(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    start: date | None = None,
    end: date | None = None,
) -> list[PriceBar]:
    """One bar per date: the best-ranked source's, with its flag and adj_close."""
    best: dict[date, PriceBar] = {}
    for src_bars in bars_by_source(conn, security_id, start, end).values():
        for b in src_bars:
            cur = best.get(b.date)
            if cur is None or _rank(b.source) < _rank(cur.source):
                best[b.date] = b
    return [best[d] for d in sorted(best)]


def save_corp_actions(conn: duckdb.DuckDBPyConnection, actions: Sequence[CorpAction]) -> None:
    rows = _act_rows(actions)
    _in_txn(conn, lambda: conn.executemany(_ACT_SQL, rows) if rows else None)


def get_corp_actions(
    conn: duckdb.DuckDBPyConnection, security_id: int, since: date | None = None
) -> list[CorpAction]:
    rows = conn.execute(
        "SELECT security_id, ex_date, kind, ratio, amount, source FROM corp_action "
        "WHERE security_id = ? AND ex_date >= COALESCE(?, DATE '0001-01-01') "
        "ORDER BY ex_date, source",
        (security_id, since),
    ).fetchall()
    acts = [
        CorpAction(security_id=r[0], ex_date=r[1], kind=r[2], ratio=r[3], amount=r[4], source=r[5])
        for r in rows
    ]
    acts.sort(key=lambda a: (a.ex_date, _rank(a.source)))
    return acts


# Bulk reads for the analysis engines (one statement per chunk of ids, never one per security) ----

CHUNK = 500


def _chunks(ids: Sequence[int]) -> list[list[int]]:
    unique = sorted(set(ids))
    return [unique[i : i + CHUNK] for i in range(0, len(unique), CHUNK)]


def _marks(chunk: Sequence[int]) -> str:
    return ", ".join("?" * len(chunk))


def get_bars_many(
    conn: duckdb.DuckDBPyConnection,
    security_ids: Sequence[int],
    start: date | None = None,
    end: date | None = None,
) -> dict[int, list[PriceBar]]:
    """`get_bars` for many securities: one bar per date (best-ranked source), oldest first. Every
    requested id is present; a security with no bars maps to an empty list."""
    best: dict[int, dict[date, PriceBar]] = {i: {} for i in security_ids}
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            f"SELECT {_BAR_COLS} FROM price_bar WHERE security_id IN ({_marks(chunk)}) "  # noqa: S608
            "AND date >= COALESCE(?, DATE '0001-01-01') AND date <= COALESCE(?, DATE '9999-12-31') "
            "ORDER BY security_id, date",
            (*chunk, start, end),
        ).fetchall()
        for r in rows:
            b = _bar(r)
            mine = best[b.security_id]
            cur = mine.get(b.date)
            if cur is None or _rank(b.source) < _rank(cur.source):
                mine[b.date] = b
    return {i: [m[d] for d in sorted(m)] for i, m in best.items()}


def get_corp_actions_many(
    conn: duckdb.DuckDBPyConnection, security_ids: Sequence[int], since: date | None = None
) -> dict[int, list[CorpAction]]:
    """`get_corp_actions` for many securities (same order within each)."""
    out: dict[int, list[CorpAction]] = {i: [] for i in security_ids}
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            "SELECT security_id, ex_date, kind, ratio, amount, source FROM corp_action "  # noqa: S608
            f"WHERE security_id IN ({_marks(chunk)}) "
            "AND ex_date >= COALESCE(?, DATE '0001-01-01') ORDER BY security_id, ex_date, source",
            (*chunk, since),
        ).fetchall()
        for r in rows:
            out[r[0]].append(
                CorpAction(
                    security_id=r[0], ex_date=r[1], kind=r[2], ratio=r[3], amount=r[4], source=r[5]
                )  # fmt: skip
            )
    for acts in out.values():
        acts.sort(key=lambda a: (a.ex_date, _rank(a.source)))
    return out


# Fundamentals and shareholding -----------------------------------------------------------------


def write_fundamentals(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    rows: Sequence[StatementRow],
    source: str,
    *,
    shareholding: Sequence[ShareholdingRow] = (),
    fetched_at: datetime | None = None,
) -> None:
    """Append-only across `filed_at` (a restatement adds a row); statements and shareholding land
    in one transaction."""
    stamp = to_iso(fetched_at or utcnow())
    f_rows = [
        (security_id, r.period_end, r.period_type, r.item, r.value, r.currency, source, r.filed_at,
         stamp)
        for r in rows
    ]  # fmt: skip
    s_rows = [
        (security_id, s.period_end, s.promoter_pct, s.promoter_pledged_pct, s.public_pct, source,
         s.filed_at)
        for s in shareholding
    ]  # fmt: skip

    def work() -> None:
        if f_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO fundamental VALUES (?,?,?,?,?,?,?,?,?)", f_rows
            )
        if s_rows:
            conn.executemany("INSERT OR REPLACE INTO shareholding VALUES (?,?,?,?,?,?,?)", s_rows)

    _in_txn(conn, work)


def get_statement_rows(conn: duckdb.DuckDBPyConnection, security_id: int) -> list[StatementRow]:
    """Every stored row (all `filed_at` versions); use `latest_as_of` for a point-in-time view."""
    rows = conn.execute(
        "SELECT period_end, period_type, item, value, currency, filed_at FROM fundamental "
        "WHERE security_id = ? ORDER BY period_end, item, filed_at",
        (security_id,),
    ).fetchall()
    return [
        StatementRow(period_end=r[0], period_type=r[1], item=r[2], value=r[3], currency=r[4] or "",
                     filed_at=r[5])
        for r in rows
    ]  # fmt: skip


def get_shareholding(conn: duckdb.DuckDBPyConnection, security_id: int) -> list[ShareholdingRow]:
    """One row per quarter (the latest filing), oldest first."""
    rows = conn.execute(
        "SELECT period_end, promoter_pct, promoter_pledged_pct, public_pct, filed_at "
        "FROM shareholding WHERE security_id = ? "
        "QUALIFY row_number() OVER (PARTITION BY period_end ORDER BY filed_at DESC) = 1 "
        "ORDER BY period_end",
        (security_id,),
    ).fetchall()
    return [
        ShareholdingRow(period_end=r[0], promoter_pct=r[1], promoter_pledged_pct=r[2],
                        public_pct=r[3], filed_at=r[4])
        for r in rows
    ]  # fmt: skip


def get_statement_rows_many(
    conn: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date | None = None
) -> dict[int, list[StatementRow]]:
    """`get_statement_rows` for many securities: every `filed_at` version, restricted to rows
    filed on or before `as_of` when given (no look-ahead)."""
    out: dict[int, list[StatementRow]] = {i: [] for i in security_ids}
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            "SELECT security_id, period_end, period_type, item, value, currency, filed_at "  # noqa: S608
            f"FROM fundamental WHERE security_id IN ({_marks(chunk)}) "
            "AND filed_at <= COALESCE(?, DATE '9999-12-31') "
            "ORDER BY security_id, period_end, item, filed_at",
            (*chunk, as_of),
        ).fetchall()
        for r in rows:
            out[r[0]].append(
                StatementRow(
                    period_end=r[1],
                    period_type=r[2],
                    item=r[3],
                    value=r[4],
                    currency=r[5] or "",
                    filed_at=r[6],
                )  # fmt: skip
            )
    return out


def get_shareholding_many(
    conn: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date | None = None
) -> dict[int, list[ShareholdingRow]]:
    """`get_shareholding` for many securities. `as_of` filters filings BEFORE the latest-per-quarter
    pick, so a restatement filed after `as_of` never hides the filing that existed on it."""
    out: dict[int, list[ShareholdingRow]] = {i: [] for i in security_ids}
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            "SELECT security_id, period_end, promoter_pct, promoter_pledged_pct, public_pct, "  # noqa: S608
            f"filed_at FROM shareholding WHERE security_id IN ({_marks(chunk)}) "
            "AND filed_at <= COALESCE(?, DATE '9999-12-31') "
            "QUALIFY row_number() OVER (PARTITION BY security_id, period_end "
            "ORDER BY filed_at DESC) = 1 ORDER BY security_id, period_end",
            (*chunk, as_of),
        ).fetchall()
        for r in rows:
            out[r[0]].append(
                ShareholdingRow(
                    period_end=r[1],
                    promoter_pct=r[2],
                    promoter_pledged_pct=r[3],
                    public_pct=r[4],
                    filed_at=r[5],
                )  # fmt: skip
            )
    return out


# Filings ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FilingRow:
    id: int
    security_id: int
    form: str
    filed_at: date
    period_end: date | None
    source: str
    url: str | None


def save_filing(
    conn: duckdb.DuckDBPyConnection,
    *,
    security_id: int,
    form: str,
    filed_at: date,
    period_end: date | None,
    source: str,
    url: str | None,
    doc_key: str,
    sections: Mapping[str, str] | None,
) -> int:
    """Filing metadata keyed by `doc_key` (idempotent) plus its named text sections."""
    conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT id FROM filing WHERE doc_key = ?", (doc_key,)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO filing (security_id, form, filed_at, period_end, source, url, doc_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (security_id, form, filed_at, period_end, source, url, doc_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("filing insert returned no id")
        fid = int(row[0])
        if sections:
            conn.executemany(
                "INSERT OR REPLACE INTO filing_section VALUES (?, ?, ?)",
                [(fid, name, text) for name, text in sections.items()],
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return fid


_FILING_COLS = "id, security_id, form, filed_at, period_end, source, url"


def list_filings(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    *,
    forms: Sequence[str] | None = None,
    since: date | None = None,
) -> list[FilingRow]:
    """Filings newest first; `forms` matches amendments too (`10-K` includes `10-K/A`)."""
    rows = conn.execute(
        f"SELECT {_FILING_COLS} FROM filing WHERE security_id = ? "  # noqa: S608
        "AND filed_at >= COALESCE(?, DATE '0001-01-01') ORDER BY filed_at DESC, id DESC",
        (security_id, since),
    ).fetchall()
    out = [FilingRow(*r) for r in rows]
    if forms:
        out = [f for f in out if f.form.removesuffix("/A") in forms]
    return out


def get_filing(conn: duckdb.DuckDBPyConnection, filing_id: int) -> FilingRow | None:
    row = conn.execute(f"SELECT {_FILING_COLS} FROM filing WHERE id = ?", (filing_id,)).fetchone()  # noqa: S608
    return None if row is None else FilingRow(*row)


def get_filing_sections(conn: duckdb.DuckDBPyConnection, filing_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT section, text FROM filing_section WHERE filing_id = ? ORDER BY section",
        (filing_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# Macro series ----------------------------------------------------------------------------------


def write_macro(
    conn: duckdb.DuckDBPyConnection,
    series_id: str,
    points: Sequence[tuple[date, Decimal]],
    source: str,
) -> None:
    rows = [(series_id, d, v, source) for d, v in points]
    _in_txn(
        conn,
        lambda: (
            conn.executemany("INSERT OR REPLACE INTO macro_series VALUES (?, ?, ?, ?)", rows)
            if rows
            else None
        ),
    )


def get_macro(
    conn: duckdb.DuckDBPyConnection,
    series_id: str,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[date, Decimal]]:
    """Dated values oldest first."""
    rows = conn.execute(
        "SELECT date, value FROM macro_series WHERE series_id = ? "
        "AND date >= COALESCE(?, DATE '0001-01-01') AND date <= COALESCE(?, DATE '9999-12-31') "
        "ORDER BY date",
        (series_id, start, end),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


# Estimates and the event calendar --------------------------------------------------------------


@dataclass(frozen=True)
class EstimateRow:
    security_id: int
    metric: str
    period: str
    value: Decimal
    as_of: date
    source: str


@dataclass(frozen=True)
class EventRow:
    security_id: int
    event_type: str
    event_date: date
    source: str


def write_estimates(
    conn: duckdb.DuckDBPyConnection,
    security_id: int,
    rows: Sequence[tuple[str, str, Decimal]],
    as_of: date,
    source: str,
) -> None:
    """Daily snapshot of (metric, period, value); re-running a day replaces that day's values."""
    data = [(security_id, m, p, v, as_of, source) for m, p, v in rows]
    _in_txn(
        conn,
        lambda: (
            conn.executemany("INSERT OR REPLACE INTO estimate VALUES (?, ?, ?, ?, ?, ?)", data)
            if data
            else None
        ),
    )


def estimate_history(
    conn: duckdb.DuckDBPyConnection, security_id: int, metric: str, period: str
) -> list[tuple[date, Decimal]]:
    rows = conn.execute(
        "SELECT as_of, value FROM estimate WHERE security_id = ? AND metric = ? AND period = ? "
        "ORDER BY as_of",
        (security_id, metric, period),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def latest_estimates(conn: duckdb.DuckDBPyConnection, security_id: int) -> list[EstimateRow]:
    """Newest snapshot per (metric, period)."""
    rows = conn.execute(
        "SELECT security_id, metric, period, value, as_of, source FROM estimate "
        "WHERE security_id = ? "
        "QUALIFY row_number() OVER (PARTITION BY metric, period ORDER BY as_of DESC) = 1 "
        "ORDER BY period, metric",
        (security_id,),
    ).fetchall()
    return [EstimateRow(*r) for r in rows]


def latest_estimates_many(
    conn: duckdb.DuckDBPyConnection, security_ids: Sequence[int], as_of: date | None = None
) -> dict[int, list[EstimateRow]]:
    """`latest_estimates` for many securities (newest snapshot on or before `as_of`)."""
    out: dict[int, list[EstimateRow]] = {i: [] for i in security_ids}
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            "SELECT security_id, metric, period, value, as_of, source FROM estimate "  # noqa: S608
            f"WHERE security_id IN ({_marks(chunk)}) "
            "AND as_of <= COALESCE(?, DATE '9999-12-31') "
            "QUALIFY row_number() OVER (PARTITION BY security_id, metric, period "
            "ORDER BY as_of DESC) = 1 ORDER BY security_id, period, metric",
            (*chunk, as_of),
        ).fetchall()
        for r in rows:
            out[r[0]].append(EstimateRow(*r))
    return out


def estimate_history_many(
    conn: duckdb.DuckDBPyConnection, security_ids: Sequence[int], since: date, until: date
) -> dict[int, list[EstimateRow]]:
    """Every estimate snapshot dated from `since` to `until`, oldest first, for many securities."""
    out: dict[int, list[EstimateRow]] = {i: [] for i in security_ids}
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            "SELECT security_id, metric, period, value, as_of, source FROM estimate "  # noqa: S608
            f"WHERE security_id IN ({_marks(chunk)}) AND as_of >= ? AND as_of <= ? "
            "ORDER BY security_id, as_of, period, metric",
            (*chunk, since, until),
        ).fetchall()
        for r in rows:
            out[r[0]].append(EstimateRow(*r))
    return out


def write_events(
    conn: duckdb.DuckDBPyConnection,
    events: Sequence[tuple[int, str, date]],
    source: str,
    as_of: date,
) -> None:
    rows = [(sid, kind, d, source, as_of) for sid, kind, d in events]
    _in_txn(
        conn,
        lambda: (
            conn.executemany("INSERT OR REPLACE INTO calendar_event VALUES (?, ?, ?, ?, ?)", rows)
            if rows
            else None
        ),
    )


def get_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    security_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    event_type: str | None = None,
) -> list[EventRow]:
    """Events sorted by date."""
    rows = conn.execute(
        "SELECT security_id, event_type, event_date, source FROM calendar_event "
        "WHERE security_id = COALESCE(?, security_id) AND event_type = COALESCE(?, event_type) "
        "AND event_date >= COALESCE(?, DATE '0001-01-01') "
        "AND event_date <= COALESCE(?, DATE '9999-12-31') ORDER BY event_date, security_id",
        (security_id, event_type, start, end),
    ).fetchall()
    return [EventRow(*r) for r in rows]


# News ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsRow:
    kind: str  # news | announcement
    published_at: datetime  # UTC
    source: str
    title: str
    url: str | None
    summary: str | None
    event_type: str | None
    materiality: str | None
    sentiment: str | None
    classified_by: str | None
    security_id: int | None = None
    id: int | None = None


def write_news(
    conn: duckdb.DuckDBPyConnection, rows: Sequence[NewsRow], fetched_at: datetime | None = None
) -> None:
    stamp = to_iso(fetched_at or utcnow())
    data = [
        (r.security_id, r.kind, to_iso(r.published_at), r.source, r.title, r.url,
         canonical_url(r.url or ""), r.summary, r.event_type, r.materiality, r.sentiment,
         r.classified_by, stamp)
        for r in rows
    ]  # fmt: skip
    sql = (
        "INSERT INTO news_item (security_id, kind, published_at, source, title, url, url_canon, "
        "summary, event_type, materiality, sentiment, classified_by, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    _in_txn(conn, lambda: conn.executemany(sql, data) if data else None)


def get_news(
    conn: duckdb.DuckDBPyConnection,
    *,
    security_id: int | None = None,
    since: datetime | None = None,
    kind: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[NewsRow]:
    """Newest first."""
    rows = conn.execute(
        "SELECT id, security_id, kind, published_at, source, title, url, summary, event_type, "
        "materiality, sentiment, classified_by FROM news_item "
        "WHERE (? IS NULL OR security_id = ?) AND (? IS NULL OR kind = ?) "
        "AND published_at >= COALESCE(?, '') "
        "ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
        (security_id, security_id, kind, kind, None if since is None else to_iso(since),
         2**31 - 1 if limit is None else limit, offset),
    ).fetchall()  # fmt: skip
    return [
        NewsRow(
            id=r[0], security_id=r[1], kind=r[2], published_at=datetime.fromisoformat(r[3]),
            source=r[4], title=r[5], url=r[6], summary=r[7], event_type=r[8], materiality=r[9],
            sentiment=r[10], classified_by=r[11],
        )
        for r in rows
    ]  # fmt: skip


@dataclass(frozen=True)
class FilingItems:
    """A stored filing with the names of its stored sections."""

    filing_id: int
    filed_at: date
    sections: tuple[str, ...]


def filing_items_many(
    conn: duckdb.DuckDBPyConnection,
    security_ids: Sequence[int],
    forms: Sequence[str],
    since: date,
    until: date,
) -> dict[int, list[FilingItems]]:
    """Filings of the given forms (amendments included) filed from `since` to `until`, that have
    at least one stored section, newest first, for many securities in one statement per chunk."""
    out: dict[int, list[FilingItems]] = {i: [] for i in security_ids}
    wanted = [*forms, *(f"{f}/A" for f in forms)]
    for chunk in _chunks(security_ids):
        rows = conn.execute(
            "SELECT f.security_id, f.id, f.filed_at, list(s.section ORDER BY s.section) "  # noqa: S608
            "FROM filing f JOIN filing_section s ON s.filing_id = f.id "
            f"WHERE f.security_id IN ({_marks(chunk)}) AND f.form IN ({_marks(wanted)}) "  # type: ignore[arg-type]
            "AND f.filed_at >= ? AND f.filed_at <= ? "
            "GROUP BY f.security_id, f.id, f.filed_at "
            "ORDER BY f.security_id, f.filed_at DESC, f.id DESC",
            (*chunk, *wanted, since, until),
        ).fetchall()
        for sid, fid, filed, names in rows:
            out[sid].append(FilingItems(int(fid), filed, tuple(names)))
    return out
