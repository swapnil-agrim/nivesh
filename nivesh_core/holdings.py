"""Standard holding / transaction schema shared by every ingestion path (FR-7).

No field can carry a name, PAN, e-mail, mobile, address, DP/client ID or folio: those never leave
the CAS parser layer. Field names avoid the redaction key parts (see ADR-0004).
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PriceBasis = Literal["previous_close", "ltp", "nav", "statement", "avg_cost"]
Source = Literal["investright", "cas_demat", "cas_rta", "alpaca", "us_csv", "csv"]
# Highest precedence first (ST-2.8). A broker-pulled US row with a market price outranks a
# book-value US CSV row.
PRECEDENCE: tuple[Source, ...] = ("investright", "cas_demat", "cas_rta", "alpaca", "us_csv", "csv")


class Holding(BaseModel):
    model_config = ConfigDict(frozen=True)

    isin: str | None
    symbol: str
    exchange: str
    name: str | None = None
    asset_class: str = "equity"
    quantity: Decimal
    avg_cost: Decimal | None = None
    price: Decimal
    price_basis: PriceBasis
    value_inr: Decimal | None
    as_of: date
    source: Source
    source_label: str
    holder_ref: str = ""
    plan: str | None = None
    amfi_code: str | None = None
    unresolved: bool = False
    currency: str = "INR"

    @model_validator(mode="after")
    def _inr_rows_carry_value(self) -> "Holding":
        if self.currency == "INR" and self.value_inr is None:
            raise ValueError("value_inr is required for an INR holding")
        return self

    @property
    def value_native(self) -> Decimal:
        """Quantity x price in the holding's own currency."""
        return self.quantity * self.price


class Lot(BaseModel):
    """A dated purchase lot (US CSV): the basis of holding period and XIRR (ST-3.4)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    exchange: str
    currency: str
    holder_ref: str = ""
    acquired_on: date
    quantity: Decimal
    cost_per_unit: Decimal
    source: Source
    source_label: str


class Txn(BaseModel):
    model_config = ConfigDict(frozen=True)

    isin: str | None
    symbol: str
    exchange: str
    name: str | None = None
    asset_class: str = "mf"
    amfi_code: str | None = None
    unresolved: bool = False
    holder_ref: str = ""
    txn_date: date
    txn_type: str
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None


class IngestReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Source
    source_label: str
    as_of: date
    holdings: int = 0
    transactions: int = 0
    lots: int = 0
    holder_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    skipped: bool = False
    ingest_id: int | None = None


class ParsedStatement(BaseModel):
    """What leaves the CAS parser layer: PII-free by construction."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["cas_demat", "cas_rta"]
    as_of: date
    holdings: list[Holding] = Field(default_factory=list)
    txns: list[Txn] = Field(default_factory=list)
    holder_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class InboxReport(BaseModel):
    """Result of scanning the CAS inbox. Files are named by a short content digest, never by their
    file name (names can carry a PAN or a person's name)."""

    model_config = ConfigDict(frozen=True)

    reports: list[IngestReport] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
