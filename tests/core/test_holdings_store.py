import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core import holdings_store as hs
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.holdings import PRECEDENCE, Holding, Lot
from tests.holdings_fx import ISIN_A, ISIN_B, D, holding, txn
from tests.us_fx import lot, usd_holding

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path / "d")
    return open_sqlite(tmp_path / "d" / "nivesh.sqlite")


def save(conn: sqlite3.Connection, hs_: list[Holding], **kw: object) -> int:
    args: dict[str, object] = {
        "kind": "investright", "source_label": "sync", "digest": None, "as_of": date(2026, 1, 5),
        "holdings": hs_, "txns": [], "holder_refs": [], "warnings": [], "now": NOW,
    }  # fmt: skip
    args.update(kw)
    rep = hs.save_ingest(conn, **args)  # type: ignore[arg-type]
    assert rep.ingest_id is not None
    return rep.ingest_id


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608


def test_save_ingest_appends_snapshots_on_every_run(conn: sqlite3.Connection) -> None:
    save(conn, [holding()])
    save(conn, [holding(quantity=D(11))])
    assert count(conn, "holding_snapshot") == 2 and count(conn, "ingest") == 2
    assert count(conn, "security") == 1 and count(conn, "account") == 1


def test_latest_holdings_returns_newest_statement_per_account_and_holder(
    conn: sqlite3.Connection,
) -> None:
    save(conn, [holding(quantity=D(1))], as_of=date(2026, 1, 5))
    save(conn, [holding(quantity=D(2))], as_of=date(2026, 1, 6))
    (h,) = hs.latest_holdings(conn)
    assert h.quantity == D(2) and h.as_of == date(2026, 1, 5)  # row as_of is the holding's own date


def test_older_statement_ingested_later_does_not_replace_newer(conn: sqlite3.Connection) -> None:
    save(conn, [holding(quantity=D(2))], as_of=date(2026, 1, 6))
    save(conn, [holding(quantity=D(1))], as_of=date(2026, 1, 5))
    (h,) = hs.latest_holdings(conn)
    assert h.quantity == D(2)


def cas(**kw: object) -> Holding:
    base = {"source": "cas_rta", "source_label": "CAS RTA", "price_basis": "nav", "isin": ISIN_B,
            "symbol": ISIN_B, "exchange": "AMFI", "asset_class": "mf"}  # fmt: skip
    base.update(kw)
    return holding(**base)


def test_fully_redeemed_folio_disappears_after_newer_statement(conn: sqlite3.Connection) -> None:
    save(
        conn,
        [cas(holder_ref="aaaa")],
        kind="cas_rta",
        holder_refs=["aaaa", "bbbb"],
        as_of=date(2026, 1, 1),
    )
    save(conn, [], kind="cas_rta", holder_refs=["aaaa", "bbbb"], as_of=date(2026, 2, 1))
    assert hs.latest_holdings(conn) == []


def test_two_depository_statements_for_different_demats_both_stay_latest(
    conn: sqlite3.Connection,
) -> None:
    def dem(ref: str) -> Holding:
        return holding(
            source="cas_demat", source_label="CAS demat", price_basis="statement", holder_ref=ref
        )

    save(conn, [dem("aaaa")], kind="cas_demat", holder_refs=["aaaa"], as_of=date(2026, 1, 1))
    save(conn, [dem("bbbb")], kind="cas_demat", holder_refs=["bbbb"], as_of=date(2026, 2, 1))
    assert sorted(h.holder_ref for h in hs.latest_holdings(conn)) == ["aaaa", "bbbb"]


def test_unresolved_isin_saved_with_isin_as_symbol_and_flag(conn: sqlite3.Connection) -> None:
    save(conn, [holding(symbol=ISIN_A, exchange="ISIN", name=None, unresolved=True)])
    row = conn.execute("select symbol, exchange, isin, unresolved from security").fetchone()
    assert row == (ISIN_A, "ISIN", ISIN_A, 1)
    (h,) = hs.latest_holdings(conn)
    assert h.unresolved and h.symbol == ISIN_A


def test_resolved_security_is_reused_not_duplicated(conn: sqlite3.Connection) -> None:
    save(conn, [holding()])
    placeholder = holding(
        source="cas_demat", source_label="CAS demat", symbol=ISIN_A, exchange="ISIN",
        unresolved=True, price_basis="statement",
    )  # fmt: skip
    save(conn, [placeholder], kind="cas_demat")
    assert count(conn, "security") == 1
    assert conn.execute("select symbol, unresolved from security").fetchone() == ("RELI", 0)


def test_placeholder_security_is_upgraded_when_resolved_later(conn: sqlite3.Connection) -> None:
    save(conn, [holding(symbol=ISIN_A, exchange="ISIN", name=None, unresolved=True)])
    save(conn, [holding()])
    assert conn.execute("select symbol, exchange, name, unresolved from security").fetchall() == [
        ("RELI", "NSE", "Reliance", 0)
    ]


def test_decimal_values_round_trip_exactly(conn: sqlite3.Connection) -> None:
    q, p = Decimal("123.456789"), Decimal("0.1")
    save(conn, [holding(quantity=q, price=p, avg_cost=Decimal("99.99"))])
    (h,) = hs.latest_holdings(conn)
    assert (h.quantity, h.price, h.avg_cost, h.value_inr) == (q, p, Decimal("99.99"), q * p)


def test_save_ingest_is_atomic_when_a_row_is_invalid(conn: sqlite3.Connection) -> None:
    bad = Holding.model_construct(**{**holding().model_dump(), "symbol": None, "isin": None})
    with pytest.raises(sqlite3.IntegrityError):
        save(conn, [holding(), bad])
    assert count(conn, "ingest") == 0 and count(conn, "holding_snapshot") == 0
    assert count(conn, "security") == 0 and not conn.in_transaction


def test_duplicate_transactions_are_ignored_on_reingest(conn: sqlite3.Connection) -> None:
    t = txn()
    save(conn, [], kind="cas_rta", txns=[t, t.model_copy()], holder_refs=["abcdefghijkl"])
    assert count(conn, "txn") == 2  # two identical same-day debits are both kept
    save(conn, [], kind="cas_rta", txns=[t, t.model_copy()], holder_refs=["abcdefghijkl"],
         digest="d2")  # fmt: skip
    assert count(conn, "txn") == 2


def test_null_units_row_is_not_duplicated_on_reingest(conn: sqlite3.Connection) -> None:
    stamp = txn(txn_type="stamp_duty", quantity=None, price=None, amount=Decimal("0.5"))
    for digest in ("a", "b"):
        save(conn, [], kind="cas_rta", txns=[stamp], holder_refs=["abcdefghijkl"], digest=digest)
    assert count(conn, "txn") == 1


def test_list_transactions_filters_by_isin_and_limit(conn: sqlite3.Connection) -> None:
    other = txn(isin=ISIN_A, symbol=ISIN_A, exchange="ISIN")
    txns = [txn(), txn(txn_date=date(2026, 1, 6)), other]
    save(conn, [], kind="cas_rta", holder_refs=["abcdefghijkl"], txns=txns)
    assert len(hs.list_transactions(conn)) == 3
    only = hs.list_transactions(conn, isin=ISIN_B)
    assert [t.txn_date for t in only] == [date(2026, 1, 6), date(2026, 1, 5)]  # newest first
    assert len(hs.list_transactions(conn, limit=1)) == 1
    assert only[0].quantity == Decimal("10.5") and only[0].holder_ref == "abcdefghijkl"


def test_already_ingested_file_digest_is_detected(conn: sqlite3.Connection) -> None:
    assert not hs.ingest_exists(conn, "cas_rta", "dig")
    save(conn, [], kind="cas_rta", digest="dig", holder_refs=["abcdefghijkl"])
    assert hs.ingest_exists(conn, "cas_rta", "dig") and not hs.ingest_exists(conn, "csv", "dig")


def test_stored_report_round_trips_counts_and_warnings(conn: sqlite3.Connection) -> None:
    iid = save(conn, [cas()], kind="cas_rta", warnings=["w1"], holder_refs=["aaaa"], txns=[txn()])
    rep = hs.get_report(conn, iid)
    assert (rep.holdings, rep.transactions, rep.warnings, rep.holder_refs) == (
        1,
        1,
        ["w1"],
        ["aaaa"],
    )
    assert rep.kind == "cas_rta" and rep.ingest_id == iid


def test_salt_fingerprint_is_recorded_then_enforced(conn: sqlite3.Connection) -> None:
    hs.check_salt_fingerprint(conn, "aaaabbbbcccc")
    hs.check_salt_fingerprint(conn, "aaaabbbbcccc")
    with pytest.raises(NiveshError, match="salt"):
        hs.check_salt_fingerprint(conn, "zzzzzzzzzzzz")


def test_last_sync_date_and_csv_accounts(conn: sqlite3.Connection) -> None:
    assert hs.last_ingest_date(conn, "investright") is None
    save(conn, [holding()])
    assert hs.last_ingest_date(conn, "investright") == date(2026, 1, 5)
    csv = holding(source="csv", source_label="Other broker", holder_ref="", price_basis="avg_cost")
    save(conn, [csv], kind="csv", digest="x")
    assert {h.source_label for h in hs.latest_holdings(conn)} == {"InvestRight", "Other broker"}


# ---- E3: currency, nullable INR value, lots ----------------------------------------------------
def test_holding_defaults_to_inr_and_requires_value_inr() -> None:
    assert holding().currency == "INR"
    with pytest.raises(ValueError, match="value_inr"):
        Holding.model_validate({**holding().model_dump(), "value_inr": None})


def test_non_inr_holding_may_have_no_value_inr() -> None:
    assert usd_holding().value_inr is None


def test_value_native_is_quantity_times_price() -> None:
    assert usd_holding(quantity=D(3), price=D("2.5")).value_native == D("7.5")


def test_precedence_places_alpaca_and_us_csv_between_cas_rta_and_csv() -> None:
    assert PRECEDENCE == ("investright", "cas_demat", "cas_rta", "alpaca", "us_csv", "csv")


def test_lot_is_frozen_and_validates() -> None:
    lt = lot()
    with pytest.raises(ValueError):
        lt.quantity = D(1)  # type: ignore[misc]
    with pytest.raises(ValueError):
        Lot.model_validate({**lt.model_dump(), "acquired_on": "not-a-date"})


def test_save_ingest_round_trips_usd_holding_with_null_value_inr(conn: sqlite3.Connection) -> None:
    save(conn, [usd_holding()], kind="us_csv")
    (h,) = hs.latest_holdings(conn)
    assert h.currency == "USD" and h.value_inr is None and h.price == D(100)


def test_save_ingest_nulls_value_inr_for_non_inr_even_if_set(conn: sqlite3.Connection) -> None:
    save(conn, [usd_holding(value_inr=D(999))], kind="us_csv")
    assert conn.execute("SELECT value_inr FROM holding_snapshot").fetchone() == (None,)


def test_upsert_security_usd_sets_market_us_and_currency_usd(conn: sqlite3.Connection) -> None:
    save(conn, [usd_holding()], kind="us_csv")
    row = conn.execute("SELECT currency, market, exchange FROM security").fetchone()
    assert row == ("USD", "US", "NASDAQ")


def test_inr_upsert_unchanged_market_in_currency_inr(conn: sqlite3.Connection) -> None:
    save(conn, [holding()])
    assert conn.execute("SELECT currency, market FROM security").fetchone() == ("INR", "IN")


def test_save_ingest_stores_lots_and_latest_lots_follow_latest_ingest_rule(
    conn: sqlite3.Connection,
) -> None:
    save(conn, [usd_holding()], kind="us_csv", lots=[lot(), lot(quantity=D(6))])
    assert [x.quantity for x in hs.latest_lots(conn)] == [D(4), D(6)]
    save(conn, [usd_holding()], kind="us_csv", lots=[lot(quantity=D(10))], as_of=date(2026, 1, 6))
    assert [x.quantity for x in hs.latest_lots(conn)] == [D(10)]


def test_reimport_edited_file_replaces_lots_not_doubles_them(conn: sqlite3.Connection) -> None:
    for q in (D(4), D(5)):
        save(conn, [usd_holding()], kind="us_csv", lots=[lot(quantity=q)])
    (only,) = hs.latest_lots(conn)
    assert only.quantity == D(5) and only.source == "us_csv"


def test_same_label_india_and_us_csv_do_not_supersede_each_other(conn: sqlite3.Connection) -> None:
    save(conn, [holding(source="csv", source_label="Broker")], kind="csv")
    save(conn, [usd_holding(source_label="Broker")], kind="us_csv")
    assert {h.currency for h in hs.latest_holdings(conn)} == {"INR", "USD"}
    kinds = {r[0] for r in conn.execute("SELECT kind FROM account")}
    assert kinds == {"manual", "manual_us"}


def test_us_csv_ingest_creates_no_manual_account(conn: sqlite3.Connection) -> None:
    save(conn, [usd_holding()], kind="us_csv")
    assert {r[0] for r in conn.execute("SELECT kind FROM account")} == {"manual_us"}


def test_save_ingest_rolls_back_lots_with_holdings(conn: sqlite3.Connection) -> None:
    with pytest.raises(NiveshError):
        save(conn, [usd_holding()], kind="us_csv", lots=[lot(symbol="ZZZ")])
    assert count(conn, "lot") == count(conn, "holding_snapshot") == count(conn, "ingest") == 0


def test_ingest_report_counts_lots(conn: sqlite3.Connection) -> None:
    rep = hs.save_ingest(
        conn, kind="us_csv", source_label="x", digest=None, as_of=date(2026, 1, 5),
        holdings=[usd_holding()], txns=[], holder_refs=[], warnings=[], lots=[lot()],
    )  # fmt: skip
    assert rep.lots == 1 and hs.get_report(conn, rep.ingest_id or 0).lots == 1
