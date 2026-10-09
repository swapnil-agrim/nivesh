import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nivesh_adapters.investright_normalise import ltp_map, normalise_holdings, parse_decimal
from nivesh_adapters.quality import DataQualityError
from nivesh_core.pii_scan import scan_text
from nivesh_core.security_resolver import Resolved
from tests.holdings_fx import D

FX = Path(__file__).resolve().parents[1] / "fixtures" / "investright"
DAY = date(2026, 1, 5)
ROWS: list[dict[str, Any]] = json.loads((FX / "holdings.json").read_text())["data"]
LTP: list[dict[str, Any]] = json.loads((FX / "ltp.json").read_text())["data"]


class Fake:
    def __init__(self, known: dict[str, Resolved]) -> None:
        self.known = known

    def resolve(self, isin: str) -> Resolved | None:
        return self.known.get(isin)


RESOLVED = Resolved(symbol="TWOCO", name="Two Co", exchange="NSE", asset_class="equity")
RESOLVER = Fake({"INE111A01011": RESOLVED})


@pytest.mark.parametrize(
    "raw, want",
    [("1,234", "1234"), ("1,23,456.50", "123456.50"), ("12.5", "12.5"), (" 7 ", "7"), (3, "3")],
)
def test_parse_decimal_strips_thousands_separators(raw: object, want: str) -> None:
    assert parse_decimal("t", "f", raw) == Decimal(want)


@pytest.mark.parametrize("raw", ["abc", "", None, "NaN", "1.2.3"])
def test_parse_decimal_rejects_garbage_with_data_quality_error(raw: object) -> None:
    with pytest.raises(DataQualityError):
        parse_decimal("t", "f", raw)


def test_blank_name_and_security_id_resolved_from_isin_via_resolver() -> None:
    h = {x.isin: x for x in normalise_holdings(ROWS, RESOLVER, {}, DAY)}["INE111A01011"]
    assert (h.symbol, h.name, h.exchange, h.unresolved) == ("TWOCO", "Two Co", "NSE", False)


def test_unresolved_isin_kept_with_isin_as_symbol_and_flagged() -> None:
    h = {x.isin: x for x in normalise_holdings(ROWS, RESOLVER, {}, DAY)}["INE999Z01019"]
    assert (h.symbol, h.exchange, h.unresolved, h.name) == ("INE999Z01019", "ISIN", True, None)


def test_close_price_only_sets_price_basis_previous_close() -> None:
    h = {x.isin: x for x in normalise_holdings(ROWS, RESOLVER, {}, DAY)}["INE222B01012"]
    assert h.price_basis == "previous_close" and h.price == D("1200.00") and h.avg_cost is None


def test_ltp_enrichment_sets_price_basis_ltp() -> None:
    by = {x.symbol: x for x in normalise_holdings(ROWS, RESOLVER, ltp_map(LTP), DAY)}
    assert by["RELI"].price_basis == "ltp" and by["RELI"].price == D("2505.75")
    assert by["TWOCO"].price_basis == "previous_close"  # not in the LTP response


def test_value_inr_is_quantity_times_price() -> None:
    h = normalise_holdings(ROWS, RESOLVER, {}, DAY)[0]
    assert h.quantity == D(1234) and h.value_inr == D(1234) * D("2500.00")


def test_avg_cost_taken_from_average_price_field() -> None:
    assert normalise_holdings(ROWS, RESOLVER, {}, DAY)[0].avg_cost == D("2450.50")


def test_zero_quantity_rows_are_dropped() -> None:
    rows = [{**ROWS[0], "quantity": "0"}, ROWS[2]]
    assert [h.isin for h in normalise_holdings(rows, RESOLVER, {}, DAY)] == ["INE222B01012"]


def test_row_without_isin_and_symbol_is_rejected_naming_the_row_index() -> None:
    rows = [ROWS[0], {"quantity": "1", "close_price": "1", "isin": "", "security_id": ""}]
    with pytest.raises(DataQualityError, match=r"rows\[1\]"):
        normalise_holdings(rows, RESOLVER, {}, DAY)


def test_row_without_any_price_is_rejected() -> None:
    with pytest.raises(DataQualityError, match="price"):
        normalise_holdings([{"isin": "INE000A01010", "quantity": "1"}], RESOLVER, {}, DAY)


def test_symbol_only_row_without_isin_is_kept() -> None:
    (h,) = normalise_holdings(
        [{"security_id": "SYMO", "name": "Sym Only", "quantity": "2", "close_price": "5"}],
        RESOLVER, {}, DAY,
    )  # fmt: skip
    assert h.isin is None and h.symbol == "SYMO" and h.exchange == "NSE" and not h.unresolved


def test_ltp_map_keys_by_security_id() -> None:
    assert ltp_map(LTP)["RELI"] == D("2505.75")


def test_normalised_holdings_contain_no_pii_patterns() -> None:
    text = " ".join(h.model_dump_json() for h in normalise_holdings(ROWS, RESOLVER, {}, DAY))
    assert scan_text(text) == []
