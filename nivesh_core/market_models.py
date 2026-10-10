"""Shared market-data row models. Money is Decimal; split ratio is new/old, bonus ratio is
new-per-held (a 1:1 bonus has ratio 1), dividend uses `amount`."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PriceBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    security_id: int
    date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal
    volume: int | None = None
    adj_close: Decimal | None = None
    source: str
    flag: str | None = None


class CorpAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    security_id: int
    ex_date: date
    kind: Literal["split", "bonus", "dividend"]
    ratio: Decimal | None = None
    amount: Decimal | None = None
    source: str


class ShareholdingRow(BaseModel):
    """Quarterly shareholding pattern; percentages of total shares (pledge: of promoter shares)."""

    model_config = ConfigDict(frozen=True)

    period_end: date
    promoter_pct: Decimal | None = None
    promoter_pledged_pct: Decimal | None = None
    public_pct: Decimal | None = None
    filed_at: date
