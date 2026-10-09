from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.csv_import import (
    ASSET_CLASSES,
    COLUMNS,
    PRESETS,
    PresetError,
    convert_preset,
    import_csv,
)  # fmt: skip
from nivesh_core.pii_scan import scan_paths
from nivesh_core.security_resolver import Resolved
from tests.holdings_fx import D

ROOT = Path(__file__).resolve().parents[2]
FX = ROOT / "tests" / "fixtures" / "csv"
DAY = date(2026, 1, 5)


class Fake:
    def __init__(self, known: dict[str, Resolved] | None = None) -> None:
        self.known = known or {}

    def resolve(self, isin: str) -> Resolved | None:
        return self.known.get(isin)


NONE = Fake()
HEADER = ",".join(COLUMNS)


def load(name: str) -> str:
    return (FX / f"{name}.csv").read_text()


def test_template_file_header_matches_code_columns() -> None:
    first = (ROOT / "templates" / "holdings.csv").read_text().splitlines()[0]
    assert (
        first
        == HEADER
        == "account_label,isin,symbol,exchange,quantity,avg_cost,currency,asset_class"
    )


def test_template_example_rows_validate() -> None:
    res = import_csv((ROOT / "templates" / "holdings.csv").read_text(), NONE, DAY)
    assert res.errors == [] and len(res.holdings) == 3


def test_valid_rows_become_manual_account_holdings() -> None:
    res = import_csv(load("valid"), NONE, DAY)
    assert res.errors == []
    labels = [h.source_label for h in res.holdings]
    assert labels == ["Other Broker"] * 3 + ["Retirement", "Bank deposits"]  # zero qty dropped
    assert all(h.source == "csv" and h.holder_ref == "" for h in res.holdings)
    assert res.holdings[0].quantity == D(1234) and res.holdings[0].avg_cost == D("2450.50")


def test_invalid_rows_are_reported_with_line_numbers() -> None:
    res = import_csv(load("invalid"), NONE, DAY)
    assert res.holdings and len(res.holdings) == 1  # the valid line 2 row is still parsed
    got = {e.line: e.message for e in res.errors}
    assert sorted(got) == [3, 4, 5, 6, 7, 8, 9, 10]
    assert "isin or a symbol" in got[3] and "quantity" in got[4] and "INR" in got[5]
    assert "asset_class" in got[6] and "negative" in got[7] and "ISIN" in got[8]
    assert "account_label" in got[9] and "avg_cost is required" in got[10]
    assert str(res.errors[0]).startswith("line 3:")


def test_row_needs_isin_or_symbol() -> None:
    res = import_csv(f"{HEADER}\nA,,,,1,1,INR,equity\n", NONE, DAY)
    assert [e.line for e in res.errors] == [2]


def test_symbol_only_row_keeps_symbol_and_exchange_and_isin_row_resolves_via_resolver() -> None:
    known = Fake(
        {
            "INE111A01011": Resolved(
                symbol="TWOCO", name="Two Co", exchange="NSE", asset_class="equity"
            )
        }
    )
    res = import_csv(load("valid"), known, DAY)
    by = {h.symbol: h for h in res.holdings}
    assert by["SYMONLY"].exchange == "BSE" and by["SYMONLY"].isin is None
    assert by["TWOCO"].name == "Two Co" and by["TWOCO"].isin == "INE111A01011"
    ph = by["INE000A01010"]  # unresolved: ISIN as symbol, flagged
    assert ph.exchange == "ISIN" and ph.unresolved


def test_non_numeric_or_negative_quantity_rejected() -> None:
    res = import_csv(
        f"{HEADER}\nA,INE000A01010,,,x,1,INR,equity\nA,INE000A01010,,,-1,1,INR,equity\n", NONE, DAY
    )
    assert [e.line for e in res.errors] == [2, 3]


def test_thousands_separators_in_quantity_and_cost_parse() -> None:
    res = import_csv(f'{HEADER}\nA,INE000A01010,,,"1,23,456","2,450.50",INR,equity\n', NONE, DAY)
    assert res.holdings[0].quantity == D(123456) and res.holdings[0].avg_cost == D("2450.50")


def test_non_inr_currency_rejected_with_e3_message() -> None:
    res = import_csv(f"{HEADER}\nA,INE000A01010,,,1,1,USD,equity\n", NONE, DAY)
    assert "E3" in res.errors[0].message


def test_unknown_asset_class_rejected() -> None:
    assert set(ASSET_CLASSES) == {"equity", "etf", "mf", "bond", "nps", "fd", "cash"}
    res = import_csv(f"{HEADER}\nA,INE000A01010,,,1,1,INR,crypto\n", NONE, DAY)
    assert "asset_class" in res.errors[0].message


@pytest.mark.parametrize(
    "header", ["account_label,isin,avg_cost", "isin,quantity", "account_label,quantity"]
)
def test_missing_required_column_reported_on_line_1(header: str) -> None:
    res = import_csv(header + "\n", NONE, DAY)
    assert [e.line for e in res.errors] == [1] and "missing column" in res.errors[0].message


def test_empty_file_reported() -> None:
    assert import_csv("", NONE, DAY).errors[0].line == 1


def test_utf8_bom_and_crlf_tolerated() -> None:
    text = "\ufeff" + f"{HEADER}\r\nA,INE000A01010,,,2,3,INR,equity\r\n\r\n"
    res = import_csv(text, NONE, DAY)
    assert res.errors == [] and len(res.holdings) == 1


def test_price_is_avg_cost_with_price_basis_avg_cost() -> None:
    (h,) = import_csv(f"{HEADER}\nA,INE000A01010,,,4,2.5,INR,equity\n", NONE, DAY).holdings
    assert (h.price, h.price_basis, h.value_inr) == (Decimal("2.5"), "avg_cost", D(10))


def test_fd_without_cost_uses_quantity_as_amount() -> None:
    (h,) = import_csv(f"{HEADER}\nBank,,FD1,,500000,,INR,fd\n", NONE, DAY).holdings
    assert h.avg_cost is None and h.value_inr == D(500000) and h.exchange == "MANUAL"


def test_template_and_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths([FX, ROOT / "templates"]) == []


@pytest.mark.parametrize("preset", ["zerodha", "groww", "upstox"])
def test_preset_converts_export_to_template_rows(preset: str) -> None:
    out = convert_preset(load(preset), preset, "My Broker")
    assert out.splitlines()[0] == HEADER
    res = import_csv(out, NONE, DAY)
    assert res.errors == [] and len(res.holdings) == 2
    assert {h.source_label for h in res.holdings} == {"My Broker"}
    first = res.holdings[0]
    assert (first.isin, first.quantity, first.avg_cost) == ("INE000A01010", D(10), D("2450.50"))


def test_zerodha_thousands_quantity_survives_conversion() -> None:
    res = import_csv(convert_preset(load("zerodha"), "zerodha", "Z"), NONE, DAY)
    assert res.holdings[1].quantity == D(1200)


def test_preset_missing_expected_column_reports_header_line_and_missing_names() -> None:
    with pytest.raises(PresetError, match=r"line 1.*quantity.*avg_cost"):
        convert_preset("Foo,Bar\n1,2\n", "groww", "G")
    with pytest.raises(PresetError, match="isin or symbol"):
        convert_preset("Qty.,Avg. cost\n1,2\n", "zerodha", "Z")


def test_unknown_preset_error_lists_available_presets() -> None:
    with pytest.raises(PresetError) as ei:
        convert_preset("", "nope", "x")
    assert all(p in str(ei.value) for p in PRESETS)


def test_preset_keeps_blank_lines_so_line_numbers_match_the_source() -> None:
    src = "ISIN,Quantity,Average buy price\n\nINE000A01010,x,5\n"
    res = import_csv(convert_preset(src, "groww", "G"), NONE, DAY)
    assert [e.line for e in res.errors] == [3]
