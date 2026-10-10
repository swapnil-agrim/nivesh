from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters import csv_import_us as m
from nivesh_adapters.csv_import import PresetError, import_csv
from nivesh_adapters.csv_import_us import convert_us_preset, import_us_csv, known_us_presets
from nivesh_core.pii_scan import scan_paths
from nivesh_core.security_resolver import Resolved

ROOT = Path(__file__).resolve().parents[2]
FX = ROOT / "tests" / "fixtures" / "csv"
TODAY = date(2026, 1, 5)
D = Decimal


class FakeResolver:
    def __init__(self, rows: dict[str, tuple[str, str]] | None = None) -> None:
        self.rows = rows or {}

    def resolve_symbol(self, symbol: str, exchange_hint: str = "") -> Resolved | None:
        if symbol not in self.rows:
            return None
        exch, name = self.rows[symbol]
        return Resolved(symbol=symbol, name=name, exchange=exch, asset_class="equity")


MASTER = FakeResolver({"MSFT": ("NASDAQ", "Microsoft Corp")})


def read(name: str) -> str:
    return (FX / name).read_text()


def run(text: str, resolver: FakeResolver = MASTER) -> m.UsCsvResult:
    return import_us_csv(text, resolver, TODAY)


def test_valid_us_csv_yields_usd_holdings_and_lots() -> None:
    r = run(read("us_valid.csv"))
    assert r.errors == []
    first = r.holdings[0]
    assert (first.symbol, first.quantity, first.avg_cost) == ("AAPL", D(10), D("150.25"))
    assert first.currency == "USD" and first.source == "us_csv" and first.value_inr is None
    assert first.price == first.avg_cost and first.price_basis == "avg_cost"
    assert first.exchange == "NASDAQ" and first.isin is None
    assert [(x.acquired_on, x.quantity) for x in r.lots] == [
        (date(2025, 3, 14), D(10)),
        (date(2025, 6, 20), D("2.5")),
        (date(2025, 1, 2), D(3)),
    ]
    assert all(x.currency == "USD" and x.source == "us_csv" for x in r.lots)


def test_master_row_sets_exchange_and_name() -> None:
    r = run(read("us_valid.csv"))
    msft = next(h for h in r.holdings if h.symbol == "MSFT")
    assert (msft.exchange, msft.name) == ("NASDAQ", "Microsoft Corp")


def test_no_master_row_uses_csv_exchange_when_nyse_nasdaq_arca() -> None:
    r = run(read("us_valid.csv"))
    etf = next(h for h in r.holdings if h.symbol == "SPYX")
    assert etf.exchange == "ARCA" and etf.asset_class == "etf" and etf.name is None


def test_no_master_row_and_no_exchange_is_line_numbered_error() -> None:
    r = run("account_label,symbol,exchange,quantity,avg_cost\nA,ZZZ,,1,2\n")
    assert [str(e) for e in r.errors] == [
        "line 2: exchange is empty and ZZZ is not in the security master; add an exchange "
        "column or run `nivesh master build`"
    ]


def test_exchange_outside_nyse_nasdaq_arca_rejected() -> None:
    r = run("account_label,symbol,exchange,quantity,avg_cost\nA,ZZZ,LSE,1,2\n")
    assert "must be one of NYSE, NASDAQ, ARCA" in str(r.errors[0])
    cboe = FakeResolver({"CBX": ("CBOE", "Cboe Name")})
    r = run("account_label,symbol,exchange,quantity,avg_cost\nA,CBX,NYSE,1,2\n", cboe)
    assert "listed on CBOE" in str(r.errors[0])


def test_non_usd_currency_rejected() -> None:
    r = run("account_label,symbol,exchange,quantity,avg_cost,currency\nA,ZZZ,NYSE,1,2,INR\n")
    assert str(r.errors[0]) == "line 2: only USD is supported here"


def test_bad_symbol_quantity_cost_date_each_line_numbered() -> None:
    r = run(read("us_invalid.csv"))
    got = {e.line: e.message for e in r.errors}
    assert sorted(got) == list(range(3, 14))
    assert "not a valid US ticker" in got[3]
    assert "quantity 'abc' is not a number" in got[4]
    assert "avg_cost must not be negative" in got[5]
    assert "avg_cost is required" in got[6]
    assert got[7] == "only USD is supported here"
    assert "asset_class" in got[8]
    assert "must be YYYY-MM-DD or MM/DD/YYYY" in got[9]
    assert "in the future" in got[10]
    assert "must be one of NYSE" in got[11]
    assert got[12] == "account_label is empty"
    assert "exchange is empty" in got[13]
    assert len(r.holdings) == 1  # only the first row was valid


def test_future_lot_date_rejected() -> None:
    r = run(
        "account_label,symbol,exchange,quantity,avg_cost,acquired_on\nA,ZZZ,NYSE,1,2,2026-01-06\n"
    )
    assert "in the future" in str(r.errors[0])


def test_both_date_formats_accepted() -> None:
    text = "account_label,symbol,exchange,quantity,avg_cost,acquired_on\n"
    text += "A,ZZZ,NYSE,1,2,2025-02-03\nA,ZZZ,NYSE,1,2,02/03/2025\n"
    r = run(text)
    assert {x.acquired_on for x in r.lots} == {date(2025, 2, 3)} and r.errors == []


def test_row_without_date_has_holding_but_no_lot() -> None:
    r = run("account_label,symbol,exchange,quantity,avg_cost\nA,ZZZ,NYSE,1,2\n")
    assert len(r.holdings) == 1 and r.lots == []


def test_fractional_quantity_and_zero_quantity_skipped() -> None:
    text = "account_label,symbol,exchange,quantity,avg_cost\nA,ZZZ,NYSE,0.25,2\nA,YYY,NYSE,0,2\n"
    r = run(text)
    assert [(h.symbol, h.quantity) for h in r.holdings] == [("ZZZ", D("0.25"))]
    assert r.errors == []
    neg = run("account_label,symbol,exchange,quantity,avg_cost\nA,ZZZ,NYSE,-1,2\n")
    assert "must not be negative" in str(neg.errors[0])


def test_missing_required_columns_reported_line_1() -> None:
    r = run("account_label,symbol\nA,ZZZ\n")
    assert [str(e) for e in r.errors] == ["line 1: missing column(s): quantity, avg_cost"]
    assert [str(e) for e in run("").errors] == ["line 1: file is empty"]


def test_bom_and_blank_lines_keep_line_numbers() -> None:
    text = (
        "﻿account_label,symbol,exchange,quantity,avg_cost\n\nA,ZZZ,NYSE,1,2\n,,,,\nA,bad!,NYSE,1,2\n"
    )
    r = run(text)
    assert [e.line for e in r.errors] == [5] and len(r.holdings) == 1


def test_template_columns_match_templates_file() -> None:
    header = (ROOT / "templates" / "holdings_us.csv").read_text().splitlines()[0]
    assert tuple(header.split(",")) == m.COLUMNS
    r = run((ROOT / "templates" / "holdings_us.csv").read_text(), FakeResolver())
    assert r.errors == [] and len(r.holdings) == 3


def test_presets_alpaca_and_robinhood_convert_and_keep_line_numbers() -> None:
    conv = convert_us_preset(read("us_alpaca.csv"), "alpaca", "My Alpaca")
    assert conv.splitlines()[0] == ",".join(m.COLUMNS) and conv.splitlines()[2] == ""
    r = run(conv)
    assert r.errors == [] and [h.symbol for h in r.holdings] == ["AAPL", "SPYX"]
    assert {h.source_label for h in r.holdings} == {"My Alpaca"}
    rh = run(convert_us_preset(read("us_robinhood.csv"), "robinhood", "RH"))
    assert [e.line for e in rh.errors] == [2] and "exchange is empty" in rh.errors[0].message
    assert [x.acquired_on for x in rh.lots] == [date(2025, 1, 2)]


def test_preset_missing_column_error_names_line_1() -> None:
    with pytest.raises(PresetError, match="line 1"):
        convert_us_preset(read("us_robinhood.csv"), "alpaca", "x")


def test_unknown_preset_lists_us_presets() -> None:
    with pytest.raises(PresetError, match="alpaca, robinhood"):
        convert_us_preset("a\n", "nope", "x")
    assert known_us_presets() == ["alpaca", "robinhood"]


def test_us_fixtures_scan_clean_of_pii() -> None:
    assert scan_paths([FX, ROOT / "templates"]) == []


def test_e2_csv_import_still_rejects_usd() -> None:
    from nivesh_core.security_resolver import TableResolver  # noqa: F401

    class NoIsin:
        def resolve(self, isin: str) -> Resolved | None:
            return None

    text = "account_label,symbol,exchange,quantity,avg_cost,currency\nA,ZZZ,NYSE,1,2,USD\n"
    r = import_csv(text, NoIsin(), TODAY)
    assert "only INR" in str(r.errors[0])
