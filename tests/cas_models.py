"""PII-laden casparser models built in code (no PDFs in the repo)."""

from decimal import Decimal
from typing import Any

from casparser.types import (
    Bond,
    CASData,
    DematAccount,
    DematOwner,
    Equity,
    Folio,
    InvestorInfo,
    MutualFund,
    NPSAccount,
    NSDLCASData,
    Scheme,
    SchemeValuation,
    StatementPeriod,
    TransactionData,
)

from tests import pii_values as pv

D = Decimal
EQ_ISIN, ETF_ISIN, MF_ISIN, BOND_ISIN = (
    "INE000A01010",
    "INF222B01012",
    "INF333C01013",
    "INE444D07014",
)
RTA_ISIN_1, RTA_ISIN_2 = "INF555E01015", "INF666F01016"


def investor() -> InvestorInfo:
    return InvestorInfo(
        name=pv.holder_name(),
        email=pv.email(),
        address=pv.person_address(),
        mobile=pv.mobile_digits(),
    )


def demat_account(client: str | None = None, **kw: Any) -> DematAccount:
    base: dict[str, Any] = {
        "name": pv.holder_name(), "type": "NSDL", "dp_id": pv.dp_code(),
        "client_id": client or pv.client_code(), "folios": 0, "balance": D(0),
        "owners": [DematOwner(name=pv.holder_name(), PAN=pv.pan())],
        "equities": [
            Equity(name="Reliance Industries", isin=EQ_ISIN, num_shares=D(10), price=D("2500.5"),
                   value=D("25005"), symbol="RELI", exchange="NSE"),
            Equity(name="Gone Ltd", isin="INE777G01017", num_shares=D(0), price=D(5), value=D(0)),
            Equity(name="Mystery ETF", isin=ETF_ISIN, num_shares=D(4), price=D(50), value=D(200)),
        ],
        "mutual_funds": [
            MutualFund(name="Demat Fund Direct", isin=MF_ISIN, balance=D("12.5"), nav=D(40),
                       value=D(500), avg_cost=D(38), amfi="100001"),
        ],
        "bonds": [Bond(name="Gov Bond", isin=BOND_ISIN, num_bonds=D(2), value=D(2000),
                       market_price=D(1000))],
    }  # fmt: skip
    base.update(kw)
    return DematAccount(**base)


def demat_data(accounts: list[DematAccount] | None = None, **kw: Any) -> NSDLCASData:
    base: dict[str, Any] = {
        "accounts": accounts if accounts is not None else [demat_account()],
        "statement_period": StatementPeriod(from_="01-Jan-2026", to="31-Jan-2026"),
        "investor_info": investor(), "file_type": "NSDL", "parse_warnings": [],
    }  # fmt: skip
    base.update(kw)
    return NSDLCASData(**base)


def nps() -> NPSAccount:
    return NPSAccount(pran=pv.client_code(), value=D(100))


def scheme(isin: str, name: str, units: str, **kw: Any) -> Scheme:
    close = D(units)
    base: dict[str, Any] = {
        "scheme": name, "rta_code": "RTA1", "rta": "CAMS", "isin": isin, "amfi": "100002",
        "open": D(0), "close": close, "close_calculated": close,
        "valuation": SchemeValuation(date="2026-01-31", nav=D(20), cost=D(1000), value=close * 20),
        "transactions": [
            TransactionData(date="2026-01-10", description="Purchase by " + pv.holder_name(),
                            amount=D(1000), units=D("50"), nav=D(20), balance=D(50),
                            type="PURCHASE_SIP"),
            TransactionData(date="2026-01-20", description="Stamp duty", amount=D("0.05"),
                            units=None, nav=None, balance=None, type="STAMP_DUTY_TAX"),
        ],
    }  # fmt: skip
    base.update(kw)
    return Scheme(**base)


def rta_data(folios: list[Folio] | None = None, **kw: Any) -> CASData:
    default = [
        Folio(folio=pv.folio_number(), amc="Some AMC", name=pv.holder_name(), PAN=pv.pan(),
              schemes=[
                  scheme(RTA_ISIN_1, "Alpha Growth Fund - Direct Plan - Growth", "50"),
                  scheme(RTA_ISIN_2, "Beta Fund - Regular Plan", "0"),
              ]),
        Folio(folio=pv.folio_number() + "/9", amc="Other AMC", schemes=[
            scheme(RTA_ISIN_2, "Beta Fund", "0", transactions=[])]),
    ]  # fmt: skip
    base: dict[str, Any] = {
        "statement_period": StatementPeriod(from_="2026-01-01", to="2026-01-31"),
        "folios": folios if folios is not None else default,
        "investor_info": investor(), "cas_type": "DETAILED", "file_type": "CAMS",
        "parse_warnings": [],
    }  # fmt: skip
    base.update(kw)
    return CASData(**base)


def pii_strings() -> list[str]:
    """Raw substrings that must never appear in any output."""
    return [
        pv.holder_name(), pv.pan(), pv.email(), pv.dp_code(), pv.client_code(),
        pv.folio_number(), pv.person_address(), pv.mobile_digits(),
    ]  # fmt: skip
