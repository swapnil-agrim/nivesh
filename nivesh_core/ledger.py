"""The call ledger (ST-12.1): one append-only row per committee call or holding review. Rows are
added and read; a correction is a new row naming the one it replaces. The database refuses
changes and removals (triggers in migrations 0007 and 0008). `as_of` is the run day and
`price_date` the date of the bar `last_close` came from; `last_close` is the split-adjusted close
as of the issue date and is for display only (ADR-0013). Decimals are stored as exact text and
read back through Decimal only. Owner decisions and score rows are in the same append-only style.
"""

import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_text
from nivesh_core.timeutil import ist_date, to_iso, utcnow

_COLS = (
    "id, run_id, security_id, verdict, horizon, conviction, suggested_weight_pct, entry_low, "
    "entry_high, entry_currency, invalidation, review_date, last_close, benchmark_level, "
    "benchmark_reason, input_hash, prompt_versions, model_versions, reported, preset, "
    "corrects_id, created_at, as_of, price_date, source"
)
VERDICT = r"^(BUY|ACCUMULATE|HOLD|TRIM|SELL|AVOID|INSUFFICIENT_DATA)$"


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: int | None = None
    run_id: int
    security_id: int
    verdict: str = Field(pattern=VERDICT)
    horizon: str = Field(pattern=r"^(positional_1_6m|long_term_1y_plus)$")
    conviction: str = Field(pattern=r"^(low|medium|high)$")
    suggested_weight_pct: Decimal | None = None
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    entry_currency: str | None = Field(default=None, pattern=r"^(INR|USD)$")
    invalidation: list[str] = []
    review_date: date
    last_close: Decimal | None = None
    benchmark_level: Decimal | None = None
    benchmark_reason: str | None = None
    input_hash: str = Field(min_length=1)
    prompt_versions: dict[str, str]
    model_versions: dict[str, str]
    reported: bool
    preset: str | None = None
    corrects_id: int | None = None
    created_at: datetime | None = None
    as_of: date | None = None
    price_date: date | None = None
    source: Literal["verdict", "holding_review"] = "verdict"

    @field_validator(
        "entry_low", "entry_high", "last_close", "benchmark_level", "suggested_weight_pct"
    )
    @classmethod
    def _finite(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not v.is_finite():
            raise ValueError("must be a finite number")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "LedgerEntry":
        if (self.entry_low is None) != (self.entry_high is None):
            raise ValueError("the entry zone needs both ends or neither")
        if self.entry_low is not None and self.entry_high is not None:
            if self.entry_low > self.entry_high:
                raise ValueError("entry zone: low is above high")
        if self.benchmark_level is None and not self.benchmark_reason:
            raise ValueError("benchmark_reason is required when benchmark_level is missing")
        return self


_INSERT = (
    f"INSERT INTO ledger_entry ({_COLS.replace('id, ', '', 1)}) "  # noqa: S608
    f"VALUES ({', '.join('?' * 24)})"
)


def _text(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


def _params(e: LedgerEntry, stamp: str) -> tuple[Any, ...]:
    return (
            e.run_id, e.security_id, e.verdict, e.horizon, e.conviction,
            _text(e.suggested_weight_pct), _text(e.entry_low), _text(e.entry_high),
            e.entry_currency, json.dumps(e.invalidation), e.review_date.isoformat(),
            _text(e.last_close), _text(e.benchmark_level), e.benchmark_reason, e.input_hash,
            json.dumps(e.prompt_versions, sort_keys=True),
            json.dumps(e.model_versions, sort_keys=True), 1 if e.reported else 0, e.preset,
            e.corrects_id, stamp, None if e.as_of is None else e.as_of.isoformat(),
            None if e.price_date is None else e.price_date.isoformat(), e.source,
        )  # fmt: skip


def record_call(conn: sqlite3.Connection, e: LedgerEntry, now: datetime | None = None) -> int:
    """Add one row and return its id."""
    cur = conn.execute(_INSERT, _params(e, to_iso(now or utcnow())))
    return int(cur.lastrowid or 0)


def record_calls(
    conn: sqlite3.Connection, entries: Sequence[LedgerEntry], now: datetime | None = None
) -> list[int]:
    """Add all rows in one transaction (all or none) and return the ids in sequence."""
    if conn.in_transaction:
        raise NiveshError("record_calls needs a connection with no open transaction")
    stamp = to_iso(now or utcnow())
    ids: list[int] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for e in entries:
            ids.append(int(conn.execute(_INSERT, _params(e, stamp)).lastrowid or 0))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ids


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _entry(r: tuple[Any, ...]) -> LedgerEntry:
    return LedgerEntry(
        id=r[0], run_id=r[1], security_id=r[2], verdict=r[3], horizon=r[4], conviction=r[5],
        suggested_weight_pct=_dec(r[6]), entry_low=_dec(r[7]), entry_high=_dec(r[8]),
        entry_currency=r[9], invalidation=json.loads(r[10]),
        review_date=date.fromisoformat(r[11]), last_close=_dec(r[12]),
        benchmark_level=_dec(r[13]), benchmark_reason=r[14], input_hash=r[15],
        prompt_versions=json.loads(r[16]), model_versions=json.loads(r[17]),
        reported=bool(r[18]), preset=r[19], corrects_id=r[20],
        created_at=datetime.fromisoformat(r[21]),
        as_of=None if r[22] is None else date.fromisoformat(r[22]),
        price_date=None if r[23] is None else date.fromisoformat(r[23]),
        source=r[24],
    )  # fmt: skip


_BY_RUN = f"SELECT {_COLS} FROM ledger_entry WHERE run_id = ? ORDER BY id"  # noqa: S608
_BY_SECURITY = f"SELECT {_COLS} FROM ledger_entry WHERE security_id = ? ORDER BY id DESC LIMIT 1"  # noqa: S608


def calls_for_run(conn: sqlite3.Connection, run_id: int) -> list[LedgerEntry]:
    return [_entry(r) for r in conn.execute(_BY_RUN, (run_id,)).fetchall()]


def latest_call(conn: sqlite3.Connection, security_id: int) -> LedgerEntry | None:
    row = conn.execute(_BY_SECURITY, (security_id,)).fetchone()
    return None if row is None else _entry(row)


# ---- owner decisions (ST-12.1: new rows that reference the original) -----------------------------
class DecisionRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: int
    entry_id: int
    decision: Literal["acted", "skipped", "partial"]
    qty: Decimal | None = None
    price: Decimal | None = None
    currency: Literal["INR", "USD"] | None = None
    note: str | None = None
    on_date: date
    decided_at: datetime
    corrects_id: int | None = None


_DEC_COLS = "id, entry_id, decision, qty, price, currency, note, on_date, decided_at, corrects_id"
_CURRENCY = {"IN": "INR", "US": "USD"}


def _decision(r: tuple[Any, ...]) -> DecisionRow:
    return DecisionRow(
        id=r[0], entry_id=r[1], decision=r[2], qty=_dec(r[3]), price=_dec(r[4]), currency=r[5],
        note=r[6], on_date=date.fromisoformat(r[7]), decided_at=datetime.fromisoformat(r[8]),
        corrects_id=r[9],
    )  # fmt: skip


def record_decision(
    conn: sqlite3.Connection, entry_id: int, decision: str, *, qty: Decimal | None = None,
    price: Decimal | None = None, currency: str | None = None, note: str | None = None,
    on_date: date | None = None, corrects_id: int | None = None, now: datetime | None = None,
) -> int:  # fmt: skip
    """Add one decision row and return its id. A correction names an earlier row of the same
    entry that no later row has corrected; nothing is ever edited."""
    when = now or utcnow()
    today = ist_date(when)
    day = on_date or today
    if day > today:
        raise NiveshError(f"the trade date {day} is in the future")
    market = conn.execute(
        "SELECT s.market FROM ledger_entry e JOIN security s ON s.id = e.security_id "
        "WHERE e.id = ?",
        (entry_id,),
    ).fetchone()
    if market is None:
        raise NiveshError(f"no ledger entry {entry_id}")
    ccy = _CURRENCY.get(market[0], "INR")
    if currency is not None and currency != ccy:
        raise NiveshError(f"currency {currency} does not match the security's market ({ccy})")
    if decision not in ("acted", "skipped", "partial"):
        raise NiveshError("decision must be acted, skipped or partial")
    if decision != "skipped":
        if qty is None or price is None or qty <= 0 or price <= 0:
            raise NiveshError(f"'{decision}' needs a positive quantity and price")
    for v in (qty, price):
        if v is not None and not v.is_finite():
            raise NiveshError("quantity and price must be finite numbers")
    if note is not None and scan_text(note):
        raise NiveshError("the note looks like it holds personal data; leave it out")
    if corrects_id is not None:
        row = conn.execute(
            "SELECT entry_id FROM ledger_decision WHERE id = ?", (corrects_id,)
        ).fetchone()
        if row is None or row[0] != entry_id:
            raise NiveshError(f"decision {corrects_id} is not a decision on entry {entry_id}")
        if conn.execute(
            "SELECT 1 FROM ledger_decision WHERE corrects_id = ?", (corrects_id,)
        ).fetchone():
            raise NiveshError(f"decision {corrects_id} was already corrected")
    cur = conn.execute(
        f"INSERT INTO ledger_decision ({_DEC_COLS.replace('id, ', '', 1)}) "  # noqa: S608
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, decision, _text(qty), _text(price), ccy, note, day.isoformat(),
         to_iso(when), corrects_id),
    )  # fmt: skip
    return int(cur.lastrowid or 0)


def effective_decisions(conn: sqlite3.Connection) -> dict[int, list[DecisionRow]]:
    """Per entry, the decision rows that count: a row corrected by a later row is left out, and
    a `skipped` row never undoes an earlier `acted` one (only a correction does)."""
    rows = [
        _decision(r)
        for r in conn.execute(f"SELECT {_DEC_COLS} FROM ledger_decision ORDER BY id").fetchall()  # noqa: S608
    ]
    corrected = {r.corrects_id for r in rows if r.corrects_id is not None}
    out: dict[int, list[DecisionRow]] = {}
    for r in rows:
        if r.id not in corrected:
            out.setdefault(r.entry_id, []).append(r)
    return out


# ---- scores (ST-12.2): one row per call, horizon and scoring version, never edited ---------
class ScoreRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: int | None = None
    entry_id: int
    horizon_weeks: Literal[1, 4, 13, 26, 52]
    status: Literal["scored", "insufficient_data"]
    ret: Decimal | None = None
    benchmark_ret: Decimal | None = None
    cost_pct: Decimal | None = None
    excess: Decimal | None = None
    call_excess: Decimal | None = None
    hit: bool | None = None
    end_date: date | None = None
    benchmark_symbol: str | None = None
    basis: str
    version: int = 1
    scored_at: datetime | None = None


_SCORE_COLS = (
    "id, entry_id, horizon_weeks, status, ret, benchmark_ret, cost_pct, excess, call_excess, hit, "
    "end_date, benchmark_symbol, basis, version, scored_at"
)


def _score(r: tuple[Any, ...]) -> ScoreRow:
    return ScoreRow(
        id=r[0], entry_id=r[1], horizon_weeks=r[2], status=r[3], ret=_dec(r[4]),
        benchmark_ret=_dec(r[5]), cost_pct=_dec(r[6]), excess=_dec(r[7]), call_excess=_dec(r[8]),
        hit=None if r[9] is None else bool(r[9]),
        end_date=None if r[10] is None else date.fromisoformat(r[10]), benchmark_symbol=r[11],
        basis=r[12], version=r[13], scored_at=datetime.fromisoformat(r[14]),
    )  # fmt: skip


def record_scores(
    conn: sqlite3.Connection, rows: Sequence[ScoreRow], now: datetime | None = None
) -> int:
    """Append score rows in one transaction and return how many were added. A scored row is
    added once per (entry, horizon, version); an insufficient row only when the key has no row
    yet, so a rerun adds nothing and a scored row may still follow an insufficient one."""
    if conn.in_transaction:
        raise NiveshError("record_scores needs a connection with no open transaction")
    stamp = to_iso(now or utcnow())
    added = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            key = (r.entry_id, r.horizon_weeks, r.version)
            if (
                r.status == "insufficient_data"
                and conn.execute(
                    "SELECT 1 FROM ledger_score WHERE entry_id = ? AND horizon_weeks = ? "
                    "AND version = ?",
                    key,
                ).fetchone()
            ):
                continue
            cur = conn.execute(
                f"INSERT INTO ledger_score ({_SCORE_COLS.replace('id, ', '', 1)}) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entry_id, horizon_weeks, version) WHERE status = 'scored' DO NOTHING",
                (r.entry_id, r.horizon_weeks, r.status, _text(r.ret), _text(r.benchmark_ret),
                 _text(r.cost_pct), _text(r.excess), _text(r.call_excess),
                 None if r.hit is None else int(r.hit),
                 None if r.end_date is None else r.end_date.isoformat(), r.benchmark_symbol,
                 r.basis, r.version, stamp),
            )  # fmt: skip
            added += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return added


def scored_keys(conn: sqlite3.Connection, version: int) -> set[tuple[int, int]]:
    """(entry id, horizon weeks) that already have a scored row for `version`."""
    return {
        (int(a), int(b))
        for a, b in conn.execute(
            "SELECT entry_id, horizon_weeks FROM ledger_score WHERE status = 'scored' "
            "AND version = ?",
            (version,),
        )
    }


def latest_scores(conn: sqlite3.Connection, version: int) -> dict[tuple[int, int], ScoreRow]:
    """The effective row per (entry id, horizon weeks): the scored row if there is one, else the
    newest row."""
    out: dict[tuple[int, int], ScoreRow] = {}
    q = f"SELECT {_SCORE_COLS} FROM ledger_score WHERE version = ? ORDER BY id"  # noqa: S608
    for raw in conn.execute(q, (version,)):
        row = _score(raw)
        key = (row.entry_id, row.horizon_weeks)
        if key not in out or out[key].status != "scored":
            out[key] = row
    return out


def list_calls(conn: sqlite3.Connection, since: date | None = None) -> list[LedgerEntry]:
    """Every ledger row, oldest first; `since` keeps rows issued on or after that date (a row
    without an `as_of` counts from the IST date it was written)."""
    rows = [_entry(r) for r in conn.execute(f"SELECT {_COLS} FROM ledger_entry ORDER BY id")]  # noqa: S608
    if since is None:
        return rows
    return [e for e in rows if (e.as_of or ist_date(e.created_at or utcnow())) >= since]
