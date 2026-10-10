"""Identifier resolution seam. The `security` table (filled by the ST-4.8 security master) answers,
and anything unresolved is kept with its ISIN as symbol and flagged."""

import sqlite3
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Resolved(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str | None
    exchange: str
    asset_class: str
    amfi_code: str | None = None


class SecurityResolver(Protocol):
    def resolve(self, isin: str) -> Resolved | None: ...


class TableResolver:
    """Resolves from rows already in the `security` table (placeholders are ignored)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def resolve(self, isin: str) -> Resolved | None:
        row = self.conn.execute(
            "SELECT symbol, name, exchange, asset_class, amfi_code FROM security "
            "WHERE isin = ? AND unresolved = 0 ORDER BY id LIMIT 1",
            (isin,),
        ).fetchone()
        if row is None:  # a renamed/merged ISIN still resolves to the current security
            row = self.conn.execute(
                "SELECT s.symbol, s.name, s.exchange, s.asset_class, s.amfi_code FROM security s "
                "JOIN security_alias a ON a.security_id = s.id "
                "WHERE a.kind = 'isin' AND a.value = ? AND s.unresolved = 0 ORDER BY s.id LIMIT 1",
                (isin,),
            ).fetchone()
        if row is None:
            return None
        return Resolved(
            symbol=row[0], name=row[1], exchange=row[2], asset_class=row[3] or "equity",
            amfi_code=row[4],
        )  # fmt: skip
