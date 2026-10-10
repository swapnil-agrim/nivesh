import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nivesh_core.db import MIGRATIONS, migrate
from nivesh_core.ledger import LedgerEntry, calls_for_run, latest_call, record_call
from nivesh_core.redact import is_sensitive_key

D = Decimal
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
SQLITE = MIGRATIONS / "sqlite"
ST_12_1 = [
    "run_id", "security_id", "verdict", "horizon", "conviction", "suggested_weight_pct",
    "entry_low", "entry_high", "invalidation", "review_date", "last_close", "benchmark_level",
    "benchmark_reason", "input_hash", "prompt_versions", "model_versions",
]  # fmt: skip


def db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    for i, s in enumerate(("A", "B"), start=1):
        c.execute(
            "INSERT INTO security (id, symbol, exchange, currency) VALUES (?, ?, 'NSE', 'INR')",
            (i, s),
        )
    for r in (1, 2):
        c.execute(
            "INSERT INTO run (id, command, started_at, status) VALUES (?, 'ideas', 't', 'ok')", (r,)
        )
    return c


def entry(**over: object) -> LedgerEntry:
    base: dict[str, object] = {
        "run_id": 1, "security_id": 1, "verdict": "BUY", "horizon": "long_term_1y_plus",
        "conviction": "high", "suggested_weight_pct": D("3.5"), "entry_low": D("100.10"),
        "entry_high": D("110.25"), "entry_currency": "INR",
        "invalidation": ["margins fall two quarters in a row"],
        "review_date": date(2027, 1, 9), "last_close": D("104.5000"),
        "benchmark_level": None, "benchmark_reason": "no benchmark configured",
        "input_hash": "ab" * 32, "prompt_versions": {"pm": "v3"},
        "model_versions": {"pm": "model-x"}, "reported": True, "preset": "lt-quality-value",
    }  # fmt: skip
    base.update(over)
    return LedgerEntry(**base)  # type: ignore[arg-type]


def test_record_call_roundtrip_keeps_decimals_exactly() -> None:
    c = db()
    eid = record_call(c, entry(), NOW)
    (back,) = calls_for_run(c, 1)
    assert back.id == eid and back.created_at == NOW
    assert back == entry().model_copy(update={"id": eid, "created_at": NOW})
    assert str(back.entry_low) == "100.10" and str(back.last_close) == "104.5000"
    raw = c.execute("SELECT entry_low, suggested_weight_pct FROM ledger_entry").fetchone()
    assert raw == ("100.10", "3.5")  # exact text; read back through Decimal only


def test_update_is_refused_by_trigger() -> None:
    c = db()
    record_call(c, entry(), NOW)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE ledger_entry SET verdict = 'HOLD'")


def test_delete_is_refused_by_trigger() -> None:
    c = db()
    record_call(c, entry(), NOW)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM ledger_entry")
    assert c.execute("SELECT count(*) FROM ledger_entry").fetchone() == (1,)


def test_correction_is_a_new_row_with_corrects_id() -> None:
    c = db()
    first = record_call(c, entry(), NOW)
    second = record_call(c, entry(verdict="HOLD", corrects_id=first), NOW)
    rows = calls_for_run(c, 1)
    assert [r.id for r in rows] == [first, second] and rows[1].corrects_id == first
    assert rows[0].verdict == "BUY"  # the original row is untouched
    with pytest.raises(sqlite3.IntegrityError):
        record_call(c, entry(corrects_id=999), NOW)


def test_calls_for_run_returns_only_that_run_sorted() -> None:
    c = db()
    a = record_call(c, entry(security_id=2), NOW)
    b = record_call(c, entry(security_id=1), NOW)
    record_call(c, entry(run_id=2), NOW)
    assert [r.id for r in calls_for_run(c, 1)] == [a, b]
    assert calls_for_run(c, 99) == []


def test_latest_call_for_security() -> None:
    c = db()
    record_call(c, entry(verdict="HOLD"), NOW)
    newest = record_call(c, entry(verdict="BUY", run_id=2), NOW)
    got = latest_call(c, 1)
    assert got is not None and got.id == newest and got.verdict == "BUY"
    assert latest_call(c, 2) is None


def test_row_carries_the_st_12_1_field_list() -> None:
    c = db()
    cols = {r[1] for r in c.execute("PRAGMA table_info(ledger_entry)")}
    want = {n for n in ST_12_1 if n not in ("entry_low", "entry_high")} | {
        "entry_low",
        "entry_high",
    }
    assert want <= cols and {"reported", "preset", "corrects_id", "created_at"} <= cols
    assert set(ST_12_1) <= set(LedgerEntry.model_fields)


def test_reported_flag_and_preset_stored() -> None:
    c = db()
    record_call(c, entry(reported=False, preset=None), NOW)
    record_call(c, entry(reported=True, preset="pos-breakout", security_id=2), NOW)
    got = calls_for_run(c, 1)
    assert [(r.reported, r.preset) for r in got] == [(False, None), (True, "pos-breakout")]


def test_benchmark_level_nullable_with_reason() -> None:
    c = db()
    record_call(c, entry(benchmark_level=D("22000.5"), benchmark_reason=None), NOW)
    with pytest.raises(ValidationError, match="benchmark_reason"):
        entry(benchmark_level=None, benchmark_reason=None)
    got = calls_for_run(c, 1)[0]
    assert got.benchmark_level == D("22000.5") and got.benchmark_reason is None


def test_entry_is_strict_and_forbids_extras() -> None:
    with pytest.raises(ValidationError):
        entry(verdict="MAYBE")
    with pytest.raises(ValidationError):
        entry(suggested_weight_pct=3.5)  # a float is not a Decimal
    with pytest.raises(ValidationError):
        entry(surprise=1)
    with pytest.raises(ValidationError, match="zone"):
        entry(entry_low=D(120), entry_high=D(110))
    with pytest.raises(ValidationError, match="zone"):
        entry(entry_low=None, entry_high=D(110))


def test_field_names_avoid_redaction_parts() -> None:
    c = db()
    cols = [r[1] for r in c.execute("PRAGMA table_info(ledger_entry)")]
    assert not [n for n in [*cols, *LedgerEntry.model_fields] if is_sensitive_key(n)]
