import json
import sqlite3
from pathlib import Path

import pytest

import nivesh_core.security_master as sm
from nivesh_core.db import MIGRATIONS, init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.security_master import build_master, load_renames, normalise_name
from tests.market_fx import mrow

X, Y = "INE002A01018", "INE467B01029"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path / "d")
    return open_sqlite(tmp_path / "d" / "nivesh.sqlite")


def one(conn: sqlite3.Connection, sql: str, *args: object) -> tuple:  # type: ignore[type-arg]
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return tuple(row)


def placeholder(conn: sqlite3.Connection, isin: str = X) -> int:
    cur = conn.execute(
        "INSERT INTO security (symbol, exchange, isin, currency, unresolved) "
        "VALUES (?, 'ISIN', ?, 'INR', 1)",
        (isin, isin),
    )
    return int(cur.lastrowid or 0)


def holder(
    conn: sqlite3.Connection, sid: int, ingest: int = 1, ref: str = "h1", txns: int = 0
) -> None:
    conn.execute("INSERT OR IGNORE INTO account (id, name, created_at) VALUES (1, 'a', 't')")
    conn.execute(
        "INSERT OR IGNORE INTO ingest (id, kind, source_label, as_of, created_at, report) "
        "VALUES (?, 'k', 'l', 'd', 't', '{}')",
        (ingest,),
    )
    conn.execute(
        "INSERT INTO holding_snapshot (ingest_id, account_id, security_id, holder_ref, quantity, "
        "price, price_basis, value_inr, as_of, source) VALUES (?, 1, ?, ?, '1', '2', 'x', '2', "
        "'d', 's')",
        (ingest, sid, ref),
    )
    for i in range(txns):
        conn.execute(
            "INSERT INTO txn (ingest_id, account_id, security_id, holder_ref, date, type, amount) "
            "VALUES (?, 1, ?, ?, '2026-01-01', 'purchase', ?)",
            (ingest, sid, ref, str(i)),
        )


def real_row(conn: sqlite3.Connection, isin: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO security (symbol, exchange, isin, currency) "
        "VALUES ('RELIANCE', 'NSE', ?, 'INR')",
        (isin,),
    )
    return int(cur.lastrowid or 0)


def ingest_row(conn: sqlite3.Connection, n: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ingest (id, kind, source_label, as_of, created_at, report) "
        "VALUES (?, 'k', 'l', 'd', 't', '{}')",
        (n,),
    )


def txn_row(conn: sqlite3.Connection, ingest: int, sid: int, amount: str = "0") -> None:
    ingest_row(conn, ingest)
    conn.execute(
        "INSERT INTO txn (ingest_id, account_id, security_id, holder_ref, date, type, amount) "
        "VALUES (?, 1, ?, 'h1', '2026-01-01', 'purchase', ?)",
        (ingest, sid, amount),
    )


def test_normalise_name_strips_suffixes_and_punctuation() -> None:
    assert normalise_name("Reliance Industries Ltd.") == "reliance industries"
    assert normalise_name("AT&T Inc") == "at and t"


def test_build_inserts_rows_with_name_norm_and_aliases(conn: sqlite3.Connection) -> None:
    s = build_master(conn, [mrow("AAPL", "NASDAQ", name="Apple Inc.", cik="320193", market="US",
                                 currency="USD")])  # fmt: skip
    assert s.inserted == 1
    assert one(conn, "SELECT name_norm, market, currency FROM security") == ("apple", "US", "USD")
    assert one(conn, "SELECT kind, value FROM security_alias") == ("cik", "320193")


def test_nse_and_bse_listing_of_one_isin_become_one_security_with_bse_alias(
    conn: sqlite3.Connection,
) -> None:
    build_master(conn, [mrow("RELI", "BSE", isin=X, bse_code="500325", industry="Refineries"),
                        mrow("RELIANCE", "NSE", isin=X)])  # fmt: skip
    assert one(conn, "SELECT count(*), symbol, exchange, industry FROM security") == (
        1, "RELIANCE", "NSE", "Refineries",
    )  # fmt: skip
    aliases = {tuple(r) for r in conn.execute("SELECT kind, value FROM security_alias")}
    assert aliases == {("symbol", "RELI"), ("bse_code", "500325")}


def test_us_ticker_row_has_cik_alias_and_usd(conn: sqlite3.Connection) -> None:
    build_master(conn, [mrow("MSFT", "NASDAQ", cik="789019", market="US", currency="USD")])
    assert one(conn, "SELECT currency, market FROM security") == ("USD", "US")


def test_build_is_idempotent(conn: sqlite3.Connection) -> None:
    rows = [mrow("RELIANCE", isin=X, bse_code="500325"), mrow("TCS", isin=Y)]
    build_master(conn, rows)
    s = build_master(conn, rows)
    assert (s.inserted, s.updated, s.upgraded, s.aliased, s.merged) == (0, 0, 0, 0, 0)
    assert one(conn, "SELECT count(*) FROM security") == (2,)


def test_placeholder_upgraded_in_place_keeps_security_id_and_snapshots(
    conn: sqlite3.Connection,
) -> None:
    p = placeholder(conn)
    holder(conn, p)
    s = build_master(conn, [mrow("RELIANCE", isin=X)])
    assert s.upgraded == 1 and s.inserted == 0
    assert one(conn, "SELECT id, symbol, exchange, unresolved FROM security") == (
        p, "RELIANCE", "NSE", 0,
    )  # fmt: skip
    assert one(conn, "SELECT security_id FROM holding_snapshot") == (p,)


def test_placeholder_merged_into_existing_real_row_repoints_snapshots_and_txn(
    conn: sqlite3.Connection,
) -> None:
    real = real_row(conn)
    p = placeholder(conn)
    holder(conn, p, ingest=1, txns=2)
    holder(conn, real, ingest=2, txns=1)  # a txn on the real row whose key collides below
    conn.execute(  # same dedup key as one placeholder txn -> collision after the repoint
        "UPDATE txn SET amount = '0', ingest_id = 1 WHERE security_id = ?", (real,)
    )
    s = build_master(conn, [mrow("RELIANCE", isin=X)])
    assert s.merged == 1 and s.rows_dropped == 1
    assert one(conn, "SELECT count(*) FROM security") == (1,)
    assert one(conn, "SELECT count(DISTINCT security_id) FROM holding_snapshot") == (1,)
    assert one(conn, "SELECT count(*) FROM txn") == (2,)
    assert one(conn, "SELECT isin FROM security") == (X,)


def test_dropped_colliding_rows_are_logged_to_master_merge_log_and_counted_in_summary(
    conn: sqlite3.Connection,
) -> None:
    real = real_row(conn)
    p = placeholder(conn)
    holder(conn, p, ingest=1, txns=1)
    holder(conn, real, ingest=2)
    txn_row(conn, 1, real)  # same dedup key as the placeholder's txn
    s = build_master(conn, [mrow("RELIANCE", isin=X)])
    assert s.rows_dropped == 1
    table, payload = one(conn, "SELECT table_name, row_json FROM master_merge_log")
    assert table == "txn" and json.loads(payload)["security_id"] == p


def test_merge_failure_rolls_back_everything(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = real_row(conn)
    p = placeholder(conn)
    holder(conn, p, txns=1)
    txn_row(conn, 1, real)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(sm, "_log_dropped", boom)
    with pytest.raises(RuntimeError):
        build_master(conn, [mrow("RELIANCE", isin=X), mrow("TCS", isin=Y)])
    assert one(conn, "SELECT count(*) FROM security") == (2,)  # nothing inserted, P still there
    assert one(conn, "SELECT count(*) FROM master_merge_log") == (0,)
    assert one(conn, "SELECT count(*) FROM txn WHERE security_id = ?", p) == (1,)
    assert not conn.in_transaction


def test_symbol_reuse_with_different_isin_is_reported_as_conflict_and_placeholder_untouched(
    conn: sqlite3.Connection,
) -> None:
    real_row(conn, Y)
    p = placeholder(conn)
    holder(conn, p)
    s = build_master(conn, [mrow("RELIANCE", isin=X)])
    assert len(s.conflicts) == 1 and X in s.conflicts[0] and Y in s.conflicts[0]
    assert one(conn, "SELECT count(*) FROM security WHERE unresolved = 1") == (1,)
    assert one(conn, "SELECT security_id FROM holding_snapshot") == (p,)


def test_same_holder_snapshots_on_placeholder_and_real_row_is_a_conflict_not_a_merge(
    conn: sqlite3.Connection,
) -> None:
    real = real_row(conn)
    p = placeholder(conn)
    holder(conn, p)
    holder(conn, real)  # same ingest/account/holder_ref: a merge would double count
    s = build_master(conn, [mrow("RELIANCE", isin=X)])
    assert s.merged == 0 and "both hold snapshots" in s.conflicts[0]
    assert one(conn, "SELECT count(*) FROM security") == (2,)


def test_nothing_references_txn_id_before_delete() -> None:
    for f in sorted((MIGRATIONS / "sqlite").glob("*.sql")):
        assert "REFERENCES txn" not in f.read_text()


def test_rename_detected_when_isin_reappears_with_new_symbol_and_old_symbol_aliased(
    conn: sqlite3.Connection,
) -> None:
    build_master(conn, [mrow("OLDNAME", isin=X)])
    s = build_master(conn, [mrow("NEWNAME", isin=X)])
    assert s.renamed == 1
    assert one(conn, "SELECT symbol FROM security") == ("NEWNAME",)
    assert one(conn, "SELECT kind, value FROM security_alias") == ("symbol", "OLDNAME")


def test_renames_yaml_maps_old_isin_to_new_security(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    f = tmp_path / "r.yaml"
    f.write_text(f"- {{old_isin: {Y}, new_isin: {X}}}\n")
    build_master(conn, [mrow("RELIANCE", isin=X)], load_renames(f))
    assert one(conn, "SELECT kind, value FROM security_alias") == ("isin", Y)


def test_renames_yaml_unknown_new_isin_is_reported_not_fatal(conn: sqlite3.Connection) -> None:
    s = build_master(conn, [mrow("RELIANCE", isin=X)], [(Y, "INE000Q01010")])
    assert len(s.unknown_renames) == 1


def test_empty_renames_file_ok_and_bad_shape_rejected(tmp_path: Path) -> None:
    from nivesh_core.errors import ConfigError

    f = tmp_path / "r.yaml"
    f.write_text("[]\n")
    assert load_renames(f) == [] and load_renames(tmp_path / "missing.yaml") == []
    f.write_text("- {oops: 1}\n")
    with pytest.raises(ConfigError):
        load_renames(f)
    assert (
        load_renames(Path(__file__).resolve().parents[2] / "config" / "security_renames.yaml") == []
    )


def test_mf_rows_follow_e2_convention_symbol_isin_exchange_amfi(conn: sqlite3.Connection) -> None:
    build_master(
        conn,
        [mrow("INF000A01011", "AMFI", isin="INF000A01011", asset_class="mf", amfi_code="100001")],
    )
    assert one(conn, "SELECT symbol, exchange, asset_class, amfi_code, isin FROM security") == (
        "INF000A01011", "AMFI", "mf", "100001", "INF000A01011",
    )  # fmt: skip


def test_name_norm_filled_for_pre_existing_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO security (symbol, exchange, name, currency) "
        "VALUES ('OLD', 'NSE', 'Old Co Ltd', 'INR')"
    )
    placeholder(conn, Y)
    build_master(conn, [])
    assert {r[0] for r in conn.execute("SELECT name_norm FROM security")} == {"old co", Y.lower()}


def test_build_summary_counts_inserted_updated_upgraded_aliased_conflicts_rows_dropped(
    conn: sqlite3.Connection,
) -> None:
    placeholder(conn)
    s = build_master(conn, [mrow("RELIANCE", isin=X, bse_code="1"), mrow("TCS", isin=Y)])
    assert (s.inserted, s.upgraded, s.aliased, s.updated, s.rows_dropped) == (1, 1, 1, 0, 0)
    s2 = build_master(conn, [mrow("TCS", isin=Y, name="TCS Renamed Limited")])
    assert s2.updated == 1


# ---- lookup --------------------------------------------------------------------------------
from nivesh_core.security_master import SecurityMaster  # noqa: E402
from nivesh_core.security_resolver import SecurityResolver  # noqa: E402
from tests.market_fx import synthetic_names  # noqa: E402


@pytest.fixture
def master(conn: sqlite3.Connection) -> SecurityMaster:
    build_master(
        conn,
        [
            mrow("RELIANCE", isin=X, name="Reliance Industries Limited", bse_code="500325"),
            mrow("RELI", "BSE", isin=X, name="Reliance Industries Limited", bse_code="500325"),
            mrow("RELIANCEPOWER", isin=Y, name="Reliance Power Limited"),
            mrow("INFY", isin="INE009A01021", name="Infosys Limited"),
            mrow("INFY", "NASDAQ", name="Infosys Ltd ADR", market="US", currency="USD"),
            mrow("AAPL", "NASDAQ", name="Apple Inc.", market="US", currency="USD", cik="320193"),
        ],
        [("INE000Z01019", X)],
    )
    return SecurityMaster(conn)


def test_lookup_by_isin_returns_single_security_id(master: SecurityMaster) -> None:
    r = master.lookup(X)
    assert r.matched_by == "isin" and r.security_id is not None
    got = master.get(r.security_id)
    assert got is not None and got.symbol == "RELIANCE"


def test_lookup_by_symbol_case_insensitive(master: SecurityMaster) -> None:
    assert master.lookup("reliance").matched_by == "symbol"
    assert master.lookup("aapl").security_id is not None


def test_lookup_by_old_isin_alias_returns_current_security(master: SecurityMaster) -> None:
    assert master.lookup("INE000Z01019").security_id == master.lookup(X).security_id


def test_lookup_by_old_symbol_alias_returns_current_security(master: SecurityMaster) -> None:
    assert master.lookup("RELI").security_id == master.lookup(X).security_id


def test_lookup_symbol_on_two_exchanges_returns_ranked_candidates(master: SecurityMaster) -> None:
    r = master.lookup("INFY")
    assert r.security_id is None and [c.exchange for c in r.candidates] == ["NSE", "NASDAQ"]


def test_lookup_market_filter_disambiguates(master: SecurityMaster) -> None:
    assert master.lookup("INFY", market="US").security_id is not None
    assert master.lookup("INFY", market="IN").security_id is not None


def test_lookup_fuzzy_name_single_match_above_threshold_and_gap(master: SecurityMaster) -> None:
    r = master.lookup("Apple Inc")
    assert r.matched_by == "name" and r.security_id is not None


def test_lookup_fuzzy_close_names_return_ranked_candidates_with_scores(
    master: SecurityMaster,
) -> None:
    r = master.lookup("reliance limited")
    assert r.security_id is None and r.matched_by == "name"
    assert [c.symbol for c in r.candidates][:2] == ["RELIANCEPOWER", "RELIANCE"]
    assert r.candidates[0].score > r.candidates[1].score
    assert all(0 < c.score <= 1 for c in r.candidates)


def test_lookup_fuzzy_suffix_words_and_punctuation_ignored(master: SecurityMaster) -> None:
    a = master.lookup("apple, inc.")
    b = master.lookup("APPLE LIMITED")
    assert a.security_id == b.security_id is not None


def test_lookup_unknown_query_returns_empty_candidates(master: SecurityMaster) -> None:
    r = master.lookup("zzzz qqqq")
    assert r.security_id is None and r.candidates == [] and master.lookup("   ").candidates == []


def test_lookup_like_wildcards_in_query_are_literal(master: SecurityMaster) -> None:
    assert master.lookup("%").candidates == [] and master.lookup("a_ple").security_id is None


def test_prefilter_caps_scored_rows_at_300(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.executemany(
        "INSERT INTO security (symbol, exchange, name, name_norm, currency) "
        "VALUES (?, 'NSE', ?, ?, 'INR')",
        [(f"S{i}", n, sm.normalise_name(n)) for i, n in enumerate(synthetic_names(20000))],
    )
    calls: list[int] = []

    class Counting(sm.SequenceMatcher):  # type: ignore[type-arg]
        def __init__(self, *a: object, **k: object) -> None:
            calls.append(1)
            super().__init__(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(sm, "SequenceMatcher", Counting)
    SecurityMaster(conn).lookup("alpha industries")
    assert 0 < len(calls) <= sm.PREFILTER_CAP


def test_two_real_rows_one_isin_lookup_returns_candidates_and_resolver_unchanged(
    conn: sqlite3.Connection,
) -> None:
    for sym, exch in (("RELI", "BSE"), ("RELIANCE", "NSE")):  # E2 can hold both
        conn.execute(
            "INSERT INTO security (symbol, exchange, isin, currency, name) "
            "VALUES (?, ?, ?, 'INR', 'R')",
            (sym, exch, X),
        )
    m = SecurityMaster(conn)
    r = m.lookup(X)
    assert r.security_id is None and [c.exchange for c in r.candidates] == ["NSE", "BSE"]
    res = m.resolve(X)
    assert res is not None and res.symbol == "RELI"  # TableResolver: ORDER BY id LIMIT 1


def test_security_master_satisfies_security_resolver_protocol(master: SecurityMaster) -> None:
    resolver: SecurityResolver = master
    assert resolver.resolve(X) is not None


def test_master_resolve_ignores_unresolved_placeholders_like_table_resolver(
    conn: sqlite3.Connection,
) -> None:
    placeholder(conn, Y)
    assert SecurityMaster(conn).resolve(Y) is None
    assert SecurityMaster(conn).lookup(Y).candidates == []


def test_cik_of_and_get_unknown(master: SecurityMaster) -> None:
    aapl = master.lookup("AAPL").security_id
    assert aapl is not None and master.cik_of(aapl) == "320193"
    assert master.cik_of(99999) is None and master.get(99999) is None


def test_fuzzy_lookup_does_not_write_and_still_finds_rows_without_name_norm(
    master: SecurityMaster, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO security (symbol, exchange, name, currency) "
        "VALUES ('OLD', 'NSE', 'Zenith Widgets Ltd', 'INR')"
    )
    conn.commit()
    before = conn.total_changes
    r = master.lookup("zenith widgets")
    assert r.candidates and r.candidates[0].symbol == "OLD"
    assert conn.total_changes == before
    assert conn.execute("SELECT name_norm FROM security WHERE symbol = 'OLD'").fetchone() == (None,)
