"""Security master (ST-4.8): build the `security` table from master rows, keep aliases for old
identifiers, upgrade/merge the ISIN placeholders E2 left behind, and resolve ISIN/symbol/name.

One canonical `security` row per ISIN that the builder creates (NSE preferred over BSE); the other
listing's symbol and the BSE scrip code become aliases. The builder never touches existing real
rows' identity: a row that already holds an ISIN under another exchange stays, and lookups return
ranked candidates for such an ISIN.
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from nivesh_core.errors import ConfigError
from nivesh_core.security_resolver import Resolved, TableResolver
from nivesh_core.timeutil import to_iso, utcnow


class MasterRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    exchange: str
    name: str | None = None
    isin: str | None = None
    currency: str = "INR"
    market: str = "IN"
    asset_class: str = "equity"
    amfi_code: str | None = None
    sector: str | None = None
    industry: str | None = None
    bse_code: str | None = None
    cik: str | None = None


SUFFIX_WORDS = {"ltd", "limited", "inc", "corp", "corporation", "plc"}


def normalise_name(name: str) -> str:
    """Lowercase, punctuation and company-suffix words removed (shared by build and lookup)."""
    text = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and "))
    return " ".join(w for w in text.split() if w not in SUFFIX_WORDS)


@dataclass
class BuildSummary:
    inserted: int = 0
    updated: int = 0
    upgraded: int = 0
    merged: int = 0
    renamed: int = 0
    aliased: int = 0
    rows_dropped: int = 0
    conflicts: list[str] = field(default_factory=list)
    unknown_renames: list[str] = field(default_factory=list)


Alias = tuple[str, str]


def _canonical(rows: Iterable[MasterRow]) -> list[tuple[MasterRow, list[Alias]]]:
    """Collapse the NSE/BSE listings of one ISIN into one row (NSE first) plus aliases."""
    groups: dict[tuple[str, ...], list[MasterRow]] = {}
    for r in rows:
        equity_isin = r.isin and r.asset_class == "equity"
        key = ("isin", r.isin or "") if equity_isin else ("row", r.exchange, r.symbol)
        groups.setdefault(key, []).append(r)
    out = []
    for group in groups.values():
        group.sort(key=lambda r: r.exchange != "NSE")  # NSE first, stable otherwise
        best = group[0]
        aliases: list[Alias] = []
        for other in group:
            if other.bse_code:
                aliases.append(("bse_code", other.bse_code))
            if other is not best:
                aliases.append(("symbol", other.symbol.upper()))
        if best.cik:
            aliases.append(("cik", best.cik))
        merged = best.model_copy(
            update={
                "bse_code": best.bse_code or next((g.bse_code for g in group if g.bse_code), None),
                "industry": best.industry or next((g.industry for g in group if g.industry), None),
            }
        )
        out.append((merged, aliases))
    return out


_SNAPSHOT = (
    "SELECT id, isin, name, name_norm, currency, asset_class, market, sector, industry, amfi_code "
    "FROM security WHERE id = ?"
)


def _values(r: MasterRow) -> tuple[object, ...]:
    return (
        r.isin, r.name, normalise_name(r.name or r.symbol), r.currency, r.asset_class, r.market,
        r.sector, r.industry, r.amfi_code,
    )  # fmt: skip


def _log_dropped(
    conn: sqlite3.Connection, placeholder: int, target: int, table: str, row: Sequence[object],
    cols: list[str], ts: str,
) -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO master_merge_log (placeholder_id, target_id, table_name, row_json, merged_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (placeholder, target, table, json.dumps(dict(zip(cols, row, strict=True))), ts),
    )


def _merge(conn: sqlite3.Connection, p: int, r: int, s: BuildSummary, ts: str) -> bool:
    """Move everything from placeholder `p` onto real row `r`; False (untouched) on a conflict."""
    double = conn.execute(
        "SELECT 1 FROM holding_snapshot a JOIN holding_snapshot b "
        "ON a.ingest_id = b.ingest_id AND a.account_id = b.account_id "
        "AND a.holder_ref = b.holder_ref WHERE a.security_id = ? AND b.security_id = ? LIMIT 1",
        (p, r),
    ).fetchone()
    if double:
        s.conflicts.append(
            f"placeholder {p} and security {r} both hold snapshots of one holder; not merged"
        )
        return False
    conn.execute("UPDATE OR IGNORE txn SET security_id = ? WHERE security_id = ?", (r, p))
    cur = conn.execute("SELECT * FROM txn WHERE security_id = ?", (p,))
    cols = [d[0] for d in cur.description]
    for row in cur.fetchall():  # duplicates of rows already on r (txn_dedup)
        _log_dropped(conn, p, r, "txn", row, cols, ts)
        s.rows_dropped += 1
    conn.execute("DELETE FROM txn WHERE security_id = ?", (p,))
    conn.execute("UPDATE holding_snapshot SET security_id = ? WHERE security_id = ?", (r, p))
    conn.execute("UPDATE lot SET security_id = ? WHERE security_id = ?", (r, p))
    conn.execute(
        "UPDATE OR IGNORE security_alias SET security_id = ? WHERE security_id = ?", (r, p)
    )
    conn.execute("DELETE FROM security_alias WHERE security_id = ?", (p,))
    conn.execute("DELETE FROM security WHERE id = ?", (p,))
    s.merged += 1
    return True


def _set(conn: sqlite3.Connection, sid: int, r: MasterRow) -> bool:
    before = conn.execute(_SNAPSHOT, (sid,)).fetchone()
    conn.execute(
        "UPDATE security SET isin = COALESCE(?, isin), name = COALESCE(?, name), "
        "name_norm = ?, currency = ?, asset_class = ?, market = ?, "
        "sector = COALESCE(?, sector), industry = COALESCE(?, industry), "
        "amfi_code = COALESCE(?, amfi_code) WHERE id = ?",
        (*_values(r), sid),
    )
    return bool(before != conn.execute(_SNAPSHOT, (sid,)).fetchone())


def _add_aliases(
    conn: sqlite3.Connection, sid: int, aliases: Sequence[Alias], s: BuildSummary
) -> None:
    for kind, value in aliases:
        cur = conn.execute(
            "INSERT OR IGNORE INTO security_alias (kind, value, security_id) VALUES (?, ?, ?)",
            (kind, value, sid),
        )
        s.aliased += cur.rowcount


def _apply(
    conn: sqlite3.Connection, r: MasterRow, aliases: list[Alias], s: BuildSummary, ts: str
) -> None:
    real = conn.execute(
        "SELECT id, isin FROM security WHERE symbol = ? AND exchange = ? AND unresolved = 0",
        (r.symbol, r.exchange),
    ).fetchone()
    ph = None
    if r.isin:
        ph = conn.execute(
            "SELECT id FROM security WHERE isin = ? AND unresolved = 1", (r.isin,)
        ).fetchone()
    if real:
        sid, cur_isin = real
        if cur_isin not in (None, r.isin):
            s.conflicts.append(
                f"{r.symbol} on {r.exchange}: stored ISIN {cur_isin} differs from {r.isin}; "
                "symbol reuse, left untouched"
            )
            return
        if ph and not _merge(conn, ph[0], sid, s, ts):
            return
        s.updated += _set(conn, sid, r)
    elif ph:
        conn.execute(
            "UPDATE security SET symbol = ?, exchange = ?, unresolved = 0 WHERE id = ?",
            (r.symbol, r.exchange, ph[0]),
        )
        _set(conn, ph[0], r)
        sid = ph[0]
        s.upgraded += 1
    else:
        same_exchange = None
        if r.isin and r.asset_class == "equity":
            same_exchange = conn.execute(
                "SELECT id, symbol FROM security "
                "WHERE isin = ? AND exchange = ? AND unresolved = 0 ORDER BY id LIMIT 1",
                (r.isin, r.exchange),
            ).fetchone()
        if same_exchange:  # known ISIN, new symbol: rename in place and keep the old symbol
            sid = same_exchange[0]
            conn.execute("UPDATE security SET symbol = ? WHERE id = ?", (r.symbol, sid))
            _add_aliases(conn, sid, [("symbol", same_exchange[1].upper())], s)
            _set(conn, sid, r)
            s.renamed += 1
        else:
            cur = conn.execute(
                "INSERT INTO security (symbol, exchange, currency) VALUES (?, ?, ?)",
                (r.symbol, r.exchange, r.currency),
            )
            sid = int(cur.lastrowid or 0)
            _set(conn, sid, r)
            s.inserted += 1
    _add_aliases(conn, sid, aliases, s)


def build_master(
    conn: sqlite3.Connection,
    rows: Iterable[MasterRow],
    renames: Sequence[tuple[str, str]] = (),
    now: datetime | None = None,
) -> BuildSummary:
    """Insert/update securities from master rows in one transaction (all or nothing).

    `renames` is (old_isin, new_isin) pairs from config/security_renames.yaml.
    """
    s, ts = BuildSummary(), to_iso(now or utcnow())
    work = _canonical(rows)
    conn.execute("BEGIN")
    try:
        for row, aliases in work:
            _apply(conn, row, aliases, s, ts)
        for old, new in renames:
            cur = conn.execute(
                "SELECT id FROM security WHERE isin = ? AND unresolved = 0 ORDER BY id LIMIT 1",
                (new,),
            ).fetchone()
            if cur is None:
                s.unknown_renames.append(f"{old} -> {new}: new ISIN not in the master")
            else:
                _add_aliases(conn, cur[0], [("isin", old)], s)
        for sid, name, symbol in conn.execute(
            "SELECT id, name, symbol FROM security WHERE name_norm IS NULL"
        ).fetchall():
            conn.execute(
                "UPDATE security SET name_norm = ? WHERE id = ?",
                (normalise_name(name or symbol), sid),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return s


def load_renames(path: Path) -> list[tuple[str, str]]:
    """Read config/security_renames.yaml: a list of {old_isin, new_isin}; missing file is empty."""
    if not path.is_file():
        return []
    import yaml

    try:
        data = yaml.safe_load(path.read_text()) or []
        return [(str(d["old_isin"]), str(d["new_isin"])) for d in data]
    except (yaml.YAMLError, KeyError, TypeError):
        raise ConfigError(f"{path}: expected a list of {{old_isin, new_isin}}") from None


# ---- lookup -------------------------------------------------------------------------------

PREFILTER_CAP = 300  # rows scored per fuzzy lookup
MIN_SCORE = 0.5  # below this a name is not even a candidate
ACCEPT_SCORE, ACCEPT_GAP = 0.92, 0.05
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_ID_RE = re.compile(r"^id:(\d+)$")
_NON_GROWTH = re.compile(r"(?i)\b(idcw|payout|dividend|reinvest\w*)\b")
_SEC_COLS = "id, symbol, exchange, name, isin, currency, asset_class, market, sector, industry"


@dataclass(frozen=True)
class SecurityRow:
    id: int
    symbol: str
    exchange: str
    name: str | None
    isin: str | None
    currency: str
    asset_class: str | None
    market: str
    sector: str | None
    industry: str | None


@dataclass(frozen=True)
class Peers:
    """Comparable securities for one security: `basis` is "industry" or "override"; `reason` says
    why the list is empty or incomplete."""

    rows: list[SecurityRow]
    basis: str
    reason: str | None = None


@dataclass(frozen=True)
class Candidate:
    security_id: int
    symbol: str
    exchange: str
    name: str | None
    score: float


@dataclass(frozen=True)
class Lookup:
    security_id: int | None
    matched_by: str | None
    candidates: list[Candidate]


def _like_escape(piece: str) -> str:
    return "%" + piece.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SecurityMaster:
    """Resolve ISIN, symbol or fuzzy name to one security id, else ranked candidates.

    Structurally a `SecurityResolver` too (delegates to `TableResolver`), so holdings code is
    unchanged.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._table = TableResolver(conn)

    def resolve(self, isin: str) -> Resolved | None:
        return self._table.resolve(isin)

    def _rows(self, where: str, args: Sequence[object], market: str | None) -> list[SecurityRow]:
        sql = f"SELECT {_SEC_COLS} FROM security WHERE unresolved = 0 AND ({where})"  # noqa: S608
        if market:
            sql += " AND market = ?"
            args = (*args, market)
        sql += " ORDER BY exchange != 'NSE', id"
        return [SecurityRow(*r) for r in self.conn.execute(sql, args).fetchall()]

    def get(self, security_id: int) -> SecurityRow | None:
        rows = self._rows("id = ?", (security_id,), None)
        return rows[0] if rows else None

    def by_symbol(self, symbol: str, market: str | None = None) -> list[SecurityRow]:
        found = self._rows("UPPER(symbol) = UPPER(?)", (symbol,), market)
        return found or self._via_alias("symbol", symbol.upper(), market)

    def by_amfi_code(self, amfi_code: str) -> SecurityRow | None:
        """The MF row for an AMFI scheme code. A scheme has one row per ISIN (growth and
        reinvest/payout): the growth-looking one wins, else the lowest id (a stable answer)."""
        rows = self._rows("amfi_code = ?", (amfi_code.strip(),), None)
        rows.sort(key=lambda r: (bool(_NON_GROWTH.search(r.name or "")), r.id))
        return rows[0] if rows else None

    def by_isin(self, isin: str) -> list[SecurityRow]:
        """Rows holding this ISIN (or whose old ISIN aliases to it); no fuzzy fallback."""
        q = isin.strip().upper()
        return self._rows("isin = ?", (q,), None) or self._via_alias("isin", q, None)

    def mf_schemes(self) -> list[tuple[str, SecurityRow]]:
        """(AMFI code, row) per scheme, the `by_amfi_code` row, ordered by code."""
        codes = self.conn.execute(
            "SELECT DISTINCT amfi_code FROM security WHERE amfi_code IS NOT NULL "
            "AND asset_class = 'mf' AND unresolved = 0 ORDER BY amfi_code"
        ).fetchall()
        found = ((str(c[0]), self.by_amfi_code(str(c[0]))) for c in codes)
        return [(code, row) for code, row in found if row is not None]

    def alias_of(self, security_id: int, kind: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM security_alias WHERE kind = ? AND security_id = ? ORDER BY value",
            (kind, security_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def cik_of(self, security_id: int) -> str | None:
        return self.alias_of(security_id, "cik")

    def get_many(self, security_ids: Iterable[int]) -> dict[int, SecurityRow]:
        """Rows by id for the ids that exist (unresolved placeholders excluded)."""
        ids = sorted(set(security_ids))
        out: dict[int, SecurityRow] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ", ".join("?" * len(chunk))
            for r in self._rows(f"id IN ({marks})", chunk, None):
                out[r.id] = r
        return out

    def peers(
        self,
        security_id: int,
        overrides: Mapping[str, Sequence[str]] | None = None,
        *,
        limit: int | None = None,
    ) -> Peers:
        """Other listed securities in the same industry and market (mutual funds and indices
        excluded), sorted by symbol. An owner override (symbol -> peer symbols, matched case
        insensitively) replaces the industry selection."""
        sec = self.get(security_id)
        if sec is None:
            return Peers([], "industry", "unknown security")
        wanted = next(
            (v for k, v in (overrides or {}).items() if k.upper() == sec.symbol.upper()), None
        )
        if wanted is not None:
            return self._override_peers(sec, wanted, limit)
        if not sec.industry:
            return Peers([], "industry", "no industry recorded for this security")
        sql = (
            f"SELECT {_SEC_COLS} FROM security WHERE industry = ? AND market = ? AND id != ? "  # noqa: S608
            "AND unresolved = 0 AND COALESCE(asset_class, '') NOT IN ('mf', 'index') "
            "ORDER BY symbol, id"
        )
        args: list[object] = [sec.industry, sec.market, sec.id]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return Peers([SecurityRow(*r) for r in self.conn.execute(sql, args).fetchall()], "industry")

    def _override_peers(self, sec: SecurityRow, wanted: Sequence[str], limit: int | None) -> Peers:
        found: dict[int, SecurityRow] = {}
        missing: list[str] = []
        for symbol in wanted:
            hits = [r for r in self.by_symbol(symbol, sec.market) if r.id != sec.id]
            if hits:
                found.setdefault(hits[0].id, hits[0])
            elif symbol.upper() != sec.symbol.upper():
                missing.append(symbol)
        rows = sorted(found.values(), key=lambda r: (r.symbol, r.id))[:limit]
        reason = (
            f"override peers not found in the master: {', '.join(missing)}" if missing else None
        )
        return Peers(rows, "override", reason)

    def _via_alias(self, kind: str, value: str, market: str | None) -> list[SecurityRow]:
        return self._rows(
            "id IN (SELECT security_id FROM security_alias WHERE kind = ? AND value = ?)",
            (kind, value),
            market,
        )

    @staticmethod
    def _exact(rows: list[SecurityRow], by: str) -> Lookup:
        cands = [Candidate(r.id, r.symbol, r.exchange, r.name, 1.0) for r in rows]
        return Lookup(rows[0].id if len(rows) == 1 else None, by, cands)

    def lookup(self, query: str, market: str | None = None) -> Lookup:
        q = query.strip()
        pinned = _ID_RE.match(q)  # "id:12": an exact, unambiguous reference to one row
        if pinned:
            row = self.get(int(pinned.group(1)))
            return self._exact([row], "id") if row else Lookup(None, None, [])
        if _ISIN_RE.match(q.upper()):
            isin = q.upper()
            rows = self._rows("isin = ?", (isin,), market) or self._via_alias("isin", isin, market)
            if rows:
                return self._exact(rows, "isin")
        rows = self.by_symbol(q, market)
        if rows:
            return self._exact(rows, "symbol")
        return self._fuzzy(q, market)

    def _prefilter(self, tokens: list[str], joiner: str, market: str | None) -> list[SecurityRow]:
        clause = joiner.join(["name_norm LIKE ? ESCAPE '\\'"] * len(tokens))
        rows = self._rows(clause, [_like_escape(t) for t in tokens], market)
        return rows[:PREFILTER_CAP]

    def _fuzzy(self, q: str, market: str | None) -> Lookup:
        nq = normalise_name(q)
        tokens = nq.split()
        if not tokens:
            return Lookup(None, None, [])
        pool = self._prefilter(tokens, " AND ", market) or self._prefilter(tokens, " OR ", market)
        # read-only path: rows not yet normalised (`master build` fills them) are scored in memory
        pool += self._rows("name_norm IS NULL", (), market)[:PREFILTER_CAP]
        norm = {
            i: n if n is not None else normalise_name(nm or sym)
            for i, n, nm, sym in self.conn.execute(
                "SELECT id, name_norm, name, symbol FROM security"
            ).fetchall()
        }
        scored = sorted(
            (
                Candidate(r.id, r.symbol, r.exchange, r.name,
                          round(SequenceMatcher(None, nq, norm[r.id] or "").ratio(), 4))
                for r in pool
            ),
            key=lambda c: -c.score,
        )  # fmt: skip
        top = [c for c in scored if c.score >= MIN_SCORE][:5]
        if (
            top
            and top[0].score >= ACCEPT_SCORE
            and (len(top) == 1 or top[0].score - top[1].score >= ACCEPT_GAP)
        ):
            return Lookup(top[0].security_id, "name", top)
        return Lookup(None, "name" if top else None, top)
