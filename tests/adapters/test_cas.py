import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from casparser.exceptions import IncorrectPasswordError

from nivesh_adapters import cas
from nivesh_adapters.cas import CasError, read_statement
from nivesh_core.holdings import ParsedStatement
from nivesh_core.pii_scan import scan_text
from nivesh_core.security_resolver import Resolved
from tests import pii_values as pv
from tests.cas_models import (
    BOND_ISIN,
    EQ_ISIN,
    ETF_ISIN,
    MF_ISIN,
    RTA_ISIN_1,
    RTA_ISIN_2,
    demat_account,
    demat_data,
    nps,
    pii_strings,
    rta_data,
)  # fmt: skip

SALT = pv.salt()
D = Decimal


class Fake:
    def __init__(self, known: dict[str, Resolved] | None = None) -> None:
        self.known = known or {}

    def resolve(self, isin: str) -> Resolved | None:
        return self.known.get(isin)


NONE = Fake()


def patch(monkeypatch: pytest.MonkeyPatch, data: Any) -> Path:
    monkeypatch.setattr(cas, "_read", lambda p, pw: data)
    return Path(__file__)  # any existing file; the parser is patched


def parse(monkeypatch: pytest.MonkeyPatch, data: Any, resolver: Fake = NONE) -> ParsedStatement:
    return read_statement(patch(monkeypatch, data), pv.cas_password(), SALT, resolver)


def by_isin(p: ParsedStatement) -> dict[str | None, Any]:
    return {h.isin: h for h in p.holdings}


def test_demat_equities_etfs_bonds_and_demat_mfs_become_holdings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got = by_isin(parse(monkeypatch, demat_data()))
    assert set(got) == {EQ_ISIN, ETF_ISIN, MF_ISIN, BOND_ISIN}
    assert got[EQ_ISIN].symbol == "RELI" and got[EQ_ISIN].quantity == D(10)
    assert got[MF_ISIN].asset_class == "mf" and got[MF_ISIN].amfi_code == "100001"
    assert got[BOND_ISIN].asset_class == "bond" and got[BOND_ISIN].price == D(1000)
    assert got[ETF_ISIN].unresolved and got[ETF_ISIN].symbol == ETF_ISIN
    assert not got[EQ_ISIN].unresolved


def test_resolver_wins_over_parser_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    master = Resolved(symbol="MASTER", name="Master Co", exchange="BSE", asset_class="etf")
    bnd = Resolved(symbol="BND", name=None, exchange="BSE", asset_class="bond")
    r = Fake({EQ_ISIN: master, BOND_ISIN: bnd})
    got = by_isin(parse(monkeypatch, demat_data(), r))
    assert (got[EQ_ISIN].symbol, got[EQ_ISIN].exchange, got[EQ_ISIN].asset_class) == (
        "MASTER", "BSE", "etf",
    )  # fmt: skip
    assert got[BOND_ISIN].symbol == "BND" and not got[BOND_ISIN].unresolved


def test_statement_date_is_period_end_date(monkeypatch: pytest.MonkeyPatch) -> None:
    p = parse(monkeypatch, demat_data())
    assert p.as_of == date(2026, 1, 31) and all(h.as_of == p.as_of for h in p.holdings)
    assert p.kind == "cas_demat"


def test_dp_and_client_ids_hash_into_holder_ref_per_demat(monkeypatch: pytest.MonkeyPatch) -> None:
    two = demat_data([demat_account(), demat_account(client="87654321")])
    p = parse(monkeypatch, two)
    refs = {h.holder_ref for h in p.holdings}
    assert len(refs) == 2 and set(p.holder_refs) == refs
    assert parse(monkeypatch, demat_data()).holder_refs[0] in refs  # same demat, same ref
    assert all(r.isalpha() and len(r) == 12 for r in refs)


def test_depository_holdings_have_no_avg_cost_and_price_basis_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got = by_isin(parse(monkeypatch, demat_data()))
    assert got[EQ_ISIN].avg_cost is None and got[EQ_ISIN].price_basis == "statement"
    assert got[EQ_ISIN].value_inr == D("25005")
    assert got[MF_ISIN].avg_cost == D(38)  # carried when the statement prints it


def test_zero_quantity_equity_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "INE777G01017" not in by_isin(parse(monkeypatch, demat_data()))


def test_nps_account_is_skipped_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    p = parse(monkeypatch, demat_data(nps=nps()))
    assert any("NPS" in w for w in p.warnings) and len(p.holdings) == 4


def test_parse_warnings_are_copied_redacted_and_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    long = "x" * 500
    p = parse(monkeypatch, demat_data(parse_warnings=["row mismatch for " + pv.email(), long]))
    assert pv.email() not in p.warnings[0] and "row mismatch" in p.warnings[0]
    assert len(p.warnings[1]) == 200


def test_depository_output_never_contains_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = demat_data().model_dump_json()
    assert scan_text(raw), "control: the raw model has PII"
    text = parse(monkeypatch, demat_data()).model_dump_json()
    assert scan_text(text) == []
    for pii in pii_strings():
        assert pii not in text


def test_scheme_with_units_becomes_holding_with_nav_value_cost_amfi_and_isin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = parse(monkeypatch, rta_data())
    (h,) = p.holdings
    assert (h.isin, h.quantity, h.price, h.value_inr) == (RTA_ISIN_1, D(50), D(20), D(1000))
    assert h.price_basis == "nav" and h.amfi_code == "100002" and h.source == "cas_rta"
    assert p.kind == "cas_rta" and p.as_of == date(2026, 1, 31) and h.as_of == p.as_of


def test_avg_cost_is_cost_over_units(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse(monkeypatch, rta_data()).holdings[0].avg_cost == D(20)


def test_plan_direct_or_regular_inferred_from_scheme_name_else_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.cas_models import Folio, scheme

    folios = [Folio(folio="f1", amc="A", schemes=[
        scheme(RTA_ISIN_1, "X Fund - Direct Plan", "5"),
        scheme(RTA_ISIN_2, "Y Fund - Regular Plan", "5"),
        scheme("INF777H01017", "Z Fund", "5")])]  # fmt: skip
    plans = {h.isin: h.plan for h in parse(monkeypatch, rta_data(folios)).holdings}
    assert plans == {RTA_ISIN_1: "direct", RTA_ISIN_2: "regular", "INF777H01017": None}


def test_zero_balance_scheme_excluded_from_holdings_but_transactions_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = parse(monkeypatch, rta_data())
    assert RTA_ISIN_2 not in by_isin(p)
    assert {t.isin for t in p.txns} == {RTA_ISIN_1, RTA_ISIN_2}


def test_transactions_map_date_type_units_nav_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    t = parse(monkeypatch, rta_data()).txns[0]
    assert (t.txn_date, t.txn_type, t.quantity, t.price, t.amount) == (
        date(2026, 1, 10), "purchase_sip", D(50), D(20), D(1000),
    )  # fmt: skip
    stamp = next(x for x in parse(monkeypatch, rta_data()).txns if x.txn_type == "stamp_duty_tax")
    assert stamp.quantity is None


def test_folio_hashed_into_holder_ref_and_absent_from_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = parse(monkeypatch, rta_data())
    assert p.holdings[0].holder_ref in p.holder_refs
    assert pv.folio_number() not in p.model_dump_json()


def test_zero_balance_folio_still_listed_in_holder_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    p = parse(monkeypatch, rta_data())
    assert len(p.holder_refs) == 2 and len({h.holder_ref for h in p.holdings}) == 1


def test_close_vs_calculated_mismatch_adds_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.cas_models import Folio, scheme

    folios = [Folio(folio="f1", amc="A", schemes=[
        scheme(RTA_ISIN_1, "X", "5", close_calculated=D(6))])]  # fmt: skip
    p = parse(monkeypatch, rta_data(folios, parse_warnings=["unit balance off"]))
    assert any("closing units differ" in w for w in p.warnings) and "unit balance off" in p.warnings
    assert p.holdings[0].quantity == D(5)


def test_rta_output_never_contains_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = rta_data().model_dump_json()
    assert scan_text(raw)
    text = parse(monkeypatch, rta_data()).model_dump_json()
    assert scan_text(text) == []
    for pii in pii_strings():
        assert pii not in text
    assert "Purchase by" not in text  # transaction descriptions are dropped


def test_rta_holding_without_isin_falls_back_to_amfi_symbol_and_resolver_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.cas_models import Folio, scheme

    folios = [Folio(folio="f1", amc="A", schemes=[
        scheme(RTA_ISIN_1, "Raw Name", "5"), scheme(None, "No Isin", "5")])]  # type: ignore[arg-type]  # fmt: skip
    r = Fake(
        {RTA_ISIN_1: Resolved(symbol="s", name="Master Name", exchange="AMFI", asset_class="mf")}
    )
    got = parse(monkeypatch, rta_data(folios), r).holdings
    assert got[0].name == "Master Name" and got[1].isin is None and got[1].symbol == "100002"


def test_unparseable_date_raises_cas_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from casparser.types import StatementPeriod

    bad = demat_data(statement_period=StatementPeriod(from_="", to="not a date"))
    with pytest.raises(CasError, match="date"):
        parse(monkeypatch, bad)


def test_read_statement_dispatches_on_returned_model_type(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse(monkeypatch, demat_data()).kind == "cas_demat"
    assert parse(monkeypatch, rta_data()).kind == "cas_rta"


def test_wrong_password_raises_cas_error_without_echoing_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(p: Path, pw: str) -> Any:
        raise IncorrectPasswordError("bad password " + pw)

    monkeypatch.setattr(cas, "_read", boom)
    with pytest.raises(CasError, match="wrong CAS password") as ei:
        read_statement(Path(__file__), pv.cas_password(), SALT, NONE)
    assert pv.cas_password() not in str(ei.value) and ei.value.__cause__ is None


def test_unexpected_parser_exception_message_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(p: Path, pw: str) -> Any:
        raise RuntimeError("page text with " + pv.holder_name())

    monkeypatch.setattr(cas, "_read", boom)
    with pytest.raises(CasError) as ei:
        read_statement(Path(__file__), "pw", SALT, NONE)
    assert pv.holder_name() not in str(ei.value) and "could not parse" in str(ei.value)


def test_unexpected_return_type_raises_cas_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CasError, match="unexpected"):
        parse(monkeypatch, {"not": "a model"})


def test_missing_file_raises_cas_error(tmp_path: Path) -> None:
    with pytest.raises(CasError, match="not found"):
        read_statement(tmp_path / "nope.pdf", "pw", SALT, NONE)


def test_read_statement_never_logs_or_prints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    parse(monkeypatch, demat_data())
    parse(monkeypatch, rta_data())
    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and caplog.text == ""


def test_read_calls_casparser_with_path_and_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import casparser

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(casparser, "read_cas_pdf", lambda f, pw: seen.append((f, pw)) or "result")
    assert cas._read(tmp_path / "a.pdf", "pw") == "result"
    assert seen == [(str(tmp_path / "a.pdf"), "pw")]


@pytest.mark.live
def test_live_round_trip_user_supplied_pdf() -> None:
    path, cas_pass = (
        os.environ.get("NIVESH_CAS_SAMPLE"),
        os.environ.get("NIVESH_CAS_SAMPLE_PASSWORD"),
    )
    if not path or not cas_pass:
        pytest.skip("set NIVESH_CAS_SAMPLE and NIVESH_CAS_SAMPLE_PASSWORD to run")
    p = read_statement(Path(path), cas_pass, SALT, NONE)
    assert scan_text(p.model_dump_json()) == []
