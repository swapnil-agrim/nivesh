"""SQLite repository for ingests, append-only snapshots and transactions (E2, ADR-0004).

One transaction per `save_ingest`. All SQL is parameterised. Decimals are stored as exact text.
"""

import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from nivesh_core.errors import NiveshError
from nivesh_core.holdings import Holding, IngestReport, Lot, Source, Txn
from nivesh_core.timeutil import to_iso, utcnow

ACCOUNT_KIND: dict[str, str] = {
    "investright": "investright",
    "cas_demat": "cas_demat",
    "cas_rta": "cas_rta",
    "csv": "manual",
    # Distinct kinds so a US account can never supersede an India account with the same label.
    "alpaca": "alpaca",
    "us_csv": "manual_us",
}
DEFAULT_ACCOUNT = {
    "investright": "InvestRight", "cas_demat": "CAS demat", "cas_rta": "CAS RTA",
    "alpaca": "Alpaca",
}  # fmt: skip


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def upsert_account(conn: sqlite3.Connection, kind: str, name: str, now: datetime) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO account (name, kind, created_at) VALUES (?, ?, ?)",
        (name, kind, to_iso(now)),
    )
    row = conn.execute(
        "SELECT id FROM account WHERE kind = ? AND name = ?", (kind, name)
    ).fetchone()
    return int(row[0])


def upsert_security(conn: sqlite3.Connection, h: Holding | Txn) -> int:
    """Reuse the row for an ISIN (or symbol+exchange); upgrade an unresolved placeholder."""
    row = None
    if h.isin:
        row = conn.execute(
            "SELECT id, unresolved FROM security WHERE isin = ? ORDER BY unresolved LIMIT 1",
            (h.isin,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id, unresolved FROM security WHERE symbol = ? AND exchange = ?",
            (h.symbol, h.exchange),
        ).fetchone()
    if row is None:
        currency = h.currency if isinstance(h, Holding) else "INR"
        cur = conn.execute(
            "INSERT INTO security (symbol, exchange, name, isin, currency, asset_class, "
            "amfi_code, unresolved, market) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (h.symbol, h.exchange, h.name, h.isin, currency, h.asset_class, h.amfi_code,
             int(h.unresolved), "US" if currency == "USD" else "IN"),
        )  # fmt: skip
        return int(cur.lastrowid or 0)
    sid = int(row[0])
    if row[1] and not h.unresolved:
        try:
            conn.execute(
                "UPDATE security SET symbol = ?, exchange = ?, name = ?, asset_class = ?, "
                "unresolved = 0 WHERE id = ?",
                (h.symbol, h.exchange, h.name, h.asset_class, sid),
            )
        except sqlite3.IntegrityError:
            pass  # ponytail: another row already owns that symbol; keep the placeholder
    if h.amfi_code:
        conn.execute(
            "UPDATE security SET amfi_code = ? WHERE id = ? AND amfi_code IS NULL",
            (h.amfi_code, sid),
        )
    return sid


def save_ingest(
    conn: sqlite3.Connection,
    *,
    kind: Source,
    source_label: str,
    digest: str | None,
    as_of: date,
    holdings: Sequence[Holding],
    txns: Sequence[Txn],
    holder_refs: Sequence[str],
    warnings: Sequence[str],
    now: datetime | None = None,
    lots: Sequence[Lot] = (),
) -> IngestReport:
    now = now or utcnow()
    report = IngestReport(
        kind=kind, source_label=source_label, as_of=as_of, holdings=len(holdings),
        transactions=len(txns), lots=len(lots), holder_refs=list(holder_refs),
        warnings=list(warnings),
    )  # fmt: skip
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO ingest (kind, source_label, digest, as_of, created_at, report) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, source_label, digest, as_of.isoformat(), to_iso(now), report.model_dump_json()),
        )
        iid = int(cur.lastrowid or 0)
        holders: set[tuple[int, str]] = set()
        default = DEFAULT_ACCOUNT.get(kind)
        if default:
            acc = upsert_account(conn, ACCOUNT_KIND[kind], default, now)
            holders |= {(acc, r) for r in (holder_refs or [""])}
        for h in holdings:
            acc = upsert_account(conn, ACCOUNT_KIND[h.source], h.source_label, now)
            holders.add((acc, h.holder_ref))
            conn.execute(
                "INSERT INTO holding_snapshot (ingest_id, account_id, security_id, holder_ref, "
                "quantity, avg_cost, price, price_basis, value_inr, as_of, source, plan, currency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iid, acc, upsert_security(conn, h), h.holder_ref, str(h.quantity),
                 _dec(h.avg_cost), str(h.price), h.price_basis, _stored_value(h),
                 h.as_of.isoformat(), h.source, h.plan, h.currency),
            )  # fmt: skip
        holders |= _save_lots(conn, iid, lots, now)
        _save_txns(conn, iid, kind, txns, now)
        conn.executemany(
            "INSERT OR IGNORE INTO ingest_holder (ingest_id, account_id, holder_ref) "
            "VALUES (?, ?, ?)",
            [(iid, a, r) for a, r in sorted(holders)],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return report.model_copy(update={"ingest_id": iid})


def _stored_value(h: Holding) -> str | None:
    """Only INR rows keep a stored INR value; other currencies are converted at read time."""
    return str(h.value_inr) if h.currency == "INR" and h.value_inr is not None else None


def _save_lots(
    conn: sqlite3.Connection, iid: int, lots: Sequence[Lot], now: datetime
) -> set[tuple[int, str]]:
    holders: set[tuple[int, str]] = set()
    for lot in lots:
        acc = upsert_account(conn, ACCOUNT_KIND[lot.source], lot.source_label, now)
        row = conn.execute(
            "SELECT id FROM security WHERE symbol = ? AND exchange = ?",
            (lot.symbol, lot.exchange),
        ).fetchone()
        if row is None:
            raise NiveshError(f"lot for {lot.symbol} has no matching holding in this ingest")
        conn.execute(
            "INSERT INTO lot (ingest_id, account_id, security_id, holder_ref, acquired_on, "
            "quantity, cost_per_unit, currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iid, acc, row[0], lot.holder_ref, lot.acquired_on.isoformat(), str(lot.quantity),
             str(lot.cost_per_unit), lot.currency),
        )  # fmt: skip
        holders.add((acc, lot.holder_ref))
    return holders


def _save_txns(
    conn: sqlite3.Connection, iid: int, kind: Source, txns: Sequence[Txn], now: datetime
) -> None:
    seen: Counter[tuple[object, ...]] = Counter()
    for t in txns:
        acc = upsert_account(conn, ACCOUNT_KIND[kind], DEFAULT_ACCOUNT.get(kind, "manual"), now)
        sid = upsert_security(conn, t)
        key = (acc, sid, t.holder_ref, t.txn_date, t.txn_type, t.quantity, t.amount)
        seen[key] += 1
        conn.execute(
            "INSERT OR IGNORE INTO txn (ingest_id, account_id, security_id, holder_ref, date, "
            "type, "
            "quantity, price, amount, occurrence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iid, acc, sid, t.holder_ref, t.txn_date.isoformat(), t.txn_type,
             str(t.quantity if t.quantity is not None else 0), _dec(t.price),
             str(t.amount if t.amount is not None else 0), seen[key] - 1),
        )  # fmt: skip


def _holding(r: tuple[object, ...]) -> Holding:
    (isin, symbol, exchange, name, asset_class, amfi, unresolved, label, holder, qty, cost, price,
     basis, value, as_of, source, plan, currency) = r  # fmt: skip
    return Holding.model_validate(
        {
            "isin": isin, "symbol": symbol, "exchange": exchange, "name": name,
            "asset_class": asset_class or "equity", "amfi_code": amfi,
            "unresolved": bool(unresolved),
            "source_label": label, "holder_ref": holder, "quantity": qty, "avg_cost": cost,
            "price": price, "price_basis": basis, "value_inr": value, "as_of": as_of,
            "source": source, "plan": plan, "currency": currency,
        }
    )  # fmt: skip


def _newest_ingests(conn: sqlite3.Connection) -> dict[tuple[int, str], int]:
    """Per (account, holder), the newest ingest that lists that holder."""
    newest: dict[tuple[int, str], int] = {}
    for iid, acc, ref in conn.execute(
        "SELECT ih.ingest_id, ih.account_id, ih.holder_ref FROM ingest_holder ih "
        "JOIN ingest i ON i.id = ih.ingest_id ORDER BY i.as_of DESC, i.id DESC"
    ):
        newest.setdefault((acc, ref), iid)
    return newest


def latest_lots(conn: sqlite3.Connection) -> list[Lot]:
    """Lots of the newest ingest per (account, holder): a re-import replaces them, never doubles."""
    out: list[Lot] = []
    for (acc, ref), iid in sorted(_newest_ingests(conn).items()):
        rows = conn.execute(
            "SELECT s.symbol, s.exchange, l.currency, l.holder_ref, l.acquired_on, l.quantity, "
            "l.cost_per_unit, a.name, i.kind FROM lot l JOIN security s ON s.id = l.security_id "
            "JOIN account a ON a.id = l.account_id JOIN ingest i ON i.id = l.ingest_id "
            "WHERE l.ingest_id = ? AND l.account_id = ? AND l.holder_ref = ? ORDER BY l.id",
            (iid, acc, ref),
        )
        out += [
            Lot.model_validate(
                {
                    "symbol": r[0],
                    "exchange": r[1],
                    "currency": r[2],
                    "holder_ref": r[3],
                    "acquired_on": r[4],
                    "quantity": r[5],
                    "cost_per_unit": r[6],
                    "source_label": r[7],
                    "source": r[8],
                }
            )  # fmt: skip
            for r in rows
        ]
    return out


def latest_holdings(conn: sqlite3.Connection) -> list[Holding]:
    """Snapshots from, per (account, holder), the newest ingest that lists that holder."""
    out: list[Holding] = []
    for (acc, ref), iid in sorted(_newest_ingests(conn).items()):
        rows = conn.execute(
            "SELECT s.isin, s.symbol, s.exchange, s.name, s.asset_class, s.amfi_code, "
            "s.unresolved, a.name, h.holder_ref, h.quantity, h.avg_cost, h.price, h.price_basis, "
            "h.value_inr, "
            "h.as_of, h.source, h.plan, h.currency FROM holding_snapshot h "
            "JOIN security s ON s.id = h.security_id JOIN account a ON a.id = h.account_id "
            "WHERE h.ingest_id = ? AND h.account_id = ? AND h.holder_ref = ? ORDER BY h.id",
            (iid, acc, ref),
        )
        out += [_holding(r) for r in rows]
    return out


def list_transactions(
    conn: sqlite3.Connection, *, isin: str | None = None, limit: int | None = None
) -> list[Txn]:
    sql = (
        "SELECT s.isin, s.symbol, s.exchange, s.name, s.asset_class, s.amfi_code, "
        "s.unresolved, t.holder_ref, t.date, t.type, t.quantity, t.price, t.amount FROM txn t "
        "JOIN security s ON s.id = t.security_id"
    )
    args: list[object] = []
    if isin:
        sql += " WHERE s.isin = ?"
        args.append(isin)
    sql += " ORDER BY t.date DESC, t.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return [
        Txn.model_validate(
            {
                "isin": r[0],
                "symbol": r[1],
                "exchange": r[2],
                "name": r[3],
                "asset_class": r[4],
                "amfi_code": r[5],
                "unresolved": bool(r[6]),
                "holder_ref": r[7],
                "txn_date": r[8],
                "txn_type": r[9],
                "quantity": r[10],
                "price": r[11],
                "amount": r[12],
            }
        )  # fmt: skip
        for r in conn.execute(sql, args)
    ]


def ingest_exists(conn: sqlite3.Connection, kind: str, digest: str) -> bool:
    row = conn.execute("SELECT 1 FROM ingest WHERE kind = ? AND digest = ?", (kind, digest))
    return row.fetchone() is not None


def get_report(conn: sqlite3.Connection, ingest_id: int) -> IngestReport:
    row = conn.execute("SELECT report FROM ingest WHERE id = ?", (ingest_id,)).fetchone()
    return IngestReport.model_validate_json(row[0]).model_copy(update={"ingest_id": ingest_id})


def last_ingest_date(conn: sqlite3.Connection, kind: str) -> date | None:
    row = conn.execute("SELECT MAX(as_of) FROM ingest WHERE kind = ?", (kind,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def check_salt_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Record the folio-salt fingerprint on first use; refuse a different salt afterwards."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'salt_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('salt_fingerprint', ?)", (fingerprint,))
    elif row[0] != fingerprint:
        raise NiveshError(
            "FOLIO_SALT differs from the salt used for earlier ingests; holder_ref values would "
            "change. Restore the original salt (or start a fresh data dir)."
        )
