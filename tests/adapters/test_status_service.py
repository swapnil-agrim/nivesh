"""Status health lines and the ingest reconciliation summary: read-only, no values leaked."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters import status_service as ss
from nivesh_adapters.investright_session import TokenStore, token_path
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from tests.holdings_fx import DAY, ISIN_A, ISIN_B, holding

NOW = datetime(2026, 1, 10, 6, 0, tzinfo=UTC)
D = Decimal


@pytest.fixture
def data(tmp_path: Path) -> Path:
    init_stores(tmp_path / "d")
    return tmp_path / "d"


def test_status_lines_hdfc_session_valid_expired_absent(data: Path) -> None:
    assert ss.session_line(data, NOW) == "hdfc session: absent; run `nivesh login`"
    store = TokenStore(token_path(data), clock=lambda: NOW - timedelta(days=2))
    store.save("not-a-real-value")
    assert ss.session_line(data, NOW) == (
        "hdfc session: expired (issued 2026-01-08); run `nivesh login`"
    )
    TokenStore(token_path(data), clock=lambda: NOW).save("not-a-real-value")
    line = ss.session_line(data, NOW)
    assert line == "hdfc session: valid (issued 2026-01-10)"
    assert "not-a-real-value" not in line


def put(data: Path, kind: str, **kw: object) -> None:
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        h = holding(source=kind, **kw)
        save_ingest(sql, kind=kind, source_label="x", digest=None, as_of=DAY,  # type: ignore[arg-type]
                    holdings=[h], txns=[], holder_refs=[""], warnings=[])  # fmt: skip
    finally:
        sql.close()


def test_last_ingest_per_source(data: Path) -> None:
    sql = open_sqlite(data / "nivesh.sqlite")
    assert ss.ingest_lines(sql) == ["last ingest: none yet; run `nivesh sync` or `nivesh ingest`"]
    sql.close()
    put(data, "investright")
    put(data, "cas_demat")
    sql = open_sqlite(data / "nivesh.sqlite")
    lines = ss.ingest_lines(sql)
    sql.close()
    assert [x.split(":")[0] for x in lines] == ["last ingest cas_demat", "last ingest investright"]
    assert all(f"as of {DAY.isoformat()}" in x for x in lines)


def test_cache_freshness_vs_ttl_stale_flagged(data: Path) -> None:
    duck = open_duck(data / "nivesh.duckdb")
    try:
        assert ss.cache_lines(duck, Settings(), NOW) == ["cache: empty"]
        for adapter, fetched in (("fresh_one", NOW - timedelta(hours=3)),
                                 ("old_one", NOW - timedelta(days=60))):  # fmt: skip
            duck.execute(
                "INSERT INTO cache_entry VALUES (?, ?, ?, ?, ?, ?)",
                (adapter, "h", json.dumps({}), "test", fetched.isoformat(), fetched.isoformat()),
            )
        lines = ss.cache_lines(duck, Settings(), NOW)
    finally:
        duck.close()
    assert lines[0].startswith("cache fresh_one: 1 entries") and "STALE" not in lines[0]
    assert "cache old_one" in lines[1] and "STALE (older than 31 days)" in lines[1]
    assert "unavailable" in ss.cache_lines(None, Settings(), NOW)[0]


def test_last_runs_with_status_and_cost(data: Path) -> None:
    sql = open_sqlite(data / "nivesh.sqlite")
    assert ss.run_lines(sql) == ["last runs: none"]
    for i in range(7):
        sql.execute(
            "INSERT INTO run (command, started_at, status, tier, cost_inr) "
            "VALUES (?, '2026-01-01T00:00:00+00:00', ?, 'deep', ?)",
            (f"c{i}", "needs_review" if i == 6 else "ok", i * 1.5),
        )
    lines = ss.run_lines(sql)
    sql.close()
    assert len(lines) == 6 and lines[1] == "  run 7 c6 needs_review (deep) cost 9.00 INR"


def test_health_is_read_only(data: Path) -> None:
    def digest() -> list[str]:
        return [hashlib.sha256((data / n).read_bytes()).hexdigest()
                for n in ("nivesh.sqlite", "nivesh.duckdb")]  # fmt: skip

    before = digest()
    sql = open_sqlite(data / "nivesh.sqlite")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    out = ss.health(sql, duck, Settings(), data, NOW)
    sql.close()
    duck.close()
    assert out[0].startswith("hdfc session") and digest() == before


def test_ingest_summary_counts_sources_and_lists_isins_never_quantities(data: Path) -> None:
    sql = open_sqlite(data / "nivesh.sqlite")
    assert ss.ingest_summary(sql, None) == ["reconciliation: no holdings stored"]
    sql.close()
    put(data, "investright", quantity=D("12345.5"))
    put(data, "cas_demat", quantity=D("12000.25"), holder_ref="abcdefghijkl")
    put(data, "cas_demat", isin=ISIN_B, symbol="OTHR", quantity=D(3), holder_ref="abcdefghijkl")
    sql = open_sqlite(data / "nivesh.sqlite")
    lines = ss.ingest_summary(sql, "abcdefghijkl")
    sql.close()
    assert lines[0].startswith("holdings by source: investright 1 (")
    assert f"2 ISIN(s) differ between InvestRight and CAS ({ISIN_A}, {ISIN_B})" in lines[1]
    assert "12345" not in "".join(lines) and "12000" not in "".join(lines)
