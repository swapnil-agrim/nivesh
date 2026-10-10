"""Mutual-fund row models (E5). NAV, TER, AUM and weights are Decimal; unknown is None, never 0.

Rows carry no security id: the store keys them by the SQLite id of the MF security row.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Plan = Literal["direct", "regular"]
Option = Literal["growth", "idcw", "other"]


class NavPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    nav: Decimal = Field(gt=0)
    source: str


class FundMeta(BaseModel):
    """Scheme metadata as of a date. TER is a percent of AUM, AUM is in rupees crore."""

    model_config = ConfigDict(frozen=True)

    as_of: date
    amfi_code: str
    scheme_name: str
    amc: str | None = None
    category: str | None = None
    plan: Plan | None = None
    option: Option | None = None
    expense_ratio: Decimal | None = None
    aum_crore: Decimal | None = None
    benchmark: str | None = None
    manager: str | None = None
    manager_since: date | None = None
    source: str


class FundHoldingRow(BaseModel):
    """One portfolio line of a fund for a month end; `other` covers cash, debt and derivatives."""

    model_config = ConfigDict(frozen=True)

    month_end: date
    isin: str
    weight_pct: Decimal = Field(ge=0, le=100)
    holding_security_id: int | None = None
    kind: Literal["equity", "other"] = "equity"
    source: str


class NavGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    gap_start: date
    gap_end: date
    missing_days: int
