"""Standard holding / transaction schema shared by every ingestion path (FR-7).

No field can carry a name, PAN, e-mail, mobile, address, DP/client ID or folio: those never leave
the CAS parser layer. Field names avoid the redaction key parts (see ADR-0004).
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PriceBasis = Literal["previous_close", "ltp", "nav", "statement", "avg_cost"]
Source = Literal["investright", "cas_demat", "cas_rta", "csv"]
# Highest precedence first (ST-2.8).
PRECEDENCE: tuple[Source, ...] = ("investright", "cas_demat", "cas_rta", "csv")


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
    value_inr: Decimal
    as_of: date
    source: Source
    source_label: str
    holder_ref: str = ""
    plan: str | None = None
    amfi_code: str | None = None
    unresolved: bool = False


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
