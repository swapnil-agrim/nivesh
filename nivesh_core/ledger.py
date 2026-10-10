"""The call ledger (ST-12.1, minimal): one append-only row per committee call. Rows are added and
read; a correction is a new row naming the one it replaces. The database refuses changes and
removals (triggers in migration 0007). Scoring and the scorecard are a later epic. Decimals are
stored as exact text and read back through Decimal only.
"""

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nivesh_core.timeutil import to_iso, utcnow

_COLS = (
    "id, run_id, security_id, verdict, horizon, conviction, suggested_weight_pct, entry_low, "
    "entry_high, entry_currency, invalidation, review_date, last_close, benchmark_level, "
    "benchmark_reason, input_hash, prompt_versions, model_versions, reported, preset, "
    "corrects_id, created_at"
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
    f"VALUES ({', '.join('?' * 21)})"
)


def _text(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


def record_call(conn: sqlite3.Connection, e: LedgerEntry, now: datetime | None = None) -> int:
    """Add one row and return its id."""
    stamp = to_iso(now or utcnow())
    cur = conn.execute(
        _INSERT,
        (
            e.run_id, e.security_id, e.verdict, e.horizon, e.conviction,
            _text(e.suggested_weight_pct), _text(e.entry_low), _text(e.entry_high),
            e.entry_currency, json.dumps(e.invalidation), e.review_date.isoformat(),
            _text(e.last_close), _text(e.benchmark_level), e.benchmark_reason, e.input_hash,
            json.dumps(e.prompt_versions, sort_keys=True),
            json.dumps(e.model_versions, sort_keys=True), 1 if e.reported else 0, e.preset,
            e.corrects_id, stamp,
        ),
    )  # fmt: skip
    return int(cur.lastrowid or 0)


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
    )  # fmt: skip


_BY_RUN = f"SELECT {_COLS} FROM ledger_entry WHERE run_id = ? ORDER BY id"  # noqa: S608
_BY_SECURITY = f"SELECT {_COLS} FROM ledger_entry WHERE security_id = ? ORDER BY id DESC LIMIT 1"  # noqa: S608


def calls_for_run(conn: sqlite3.Connection, run_id: int) -> list[LedgerEntry]:
    return [_entry(r) for r in conn.execute(_BY_RUN, (run_id,)).fetchall()]


def latest_call(conn: sqlite3.Connection, security_id: int) -> LedgerEntry | None:
    row = conn.execute(_BY_SECURITY, (security_id,)).fetchone()
    return None if row is None else _entry(row)
