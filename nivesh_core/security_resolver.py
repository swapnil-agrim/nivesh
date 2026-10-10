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


# US venues accepted for import (BR-6). AMEX / NYSE American trade under the NYSE label.
US_EXCHANGE_ALIASES: dict[str, str] = {
    "NYSE": "NYSE", "NASDAQ": "NASDAQ", "ARCA": "ARCA", "NYSE ARCA": "ARCA", "NYSEARCA": "ARCA",
    "AMEX": "NYSE", "NYSE AMERICAN": "NYSE",
}  # fmt: skip


def normalise_us_exchange(raw: str) -> str | None:
    """NYSE, NASDAQ or ARCA for a known spelling (case-insensitive), else None."""
    return US_EXCHANGE_ALIASES.get(" ".join(raw.upper().split()))


def _fold(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-")


class UsSymbolResolver:
    """Resolves a US ticker from `security` rows of market US (placeholders and indices ignored).

    The master's exchange is authoritative. Several exchanges for one symbol resolve only with a
    matching hint, never by guessing. A row on CBOE/OTC/US still resolves; the caller decides
    whether that venue is importable (`normalise_us_exchange`)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def resolve_symbol(self, symbol: str, exchange_hint: str = "") -> Resolved | None:
        rows = self.conn.execute(
            "SELECT symbol, name, exchange, asset_class FROM security WHERE market = 'US' "
            "AND unresolved = 0 AND COALESCE(asset_class, '') != 'index' "
            "AND REPLACE(UPPER(symbol), '.', '-') = ? ORDER BY id",
            (_fold(symbol),),
        ).fetchall()
        if len({r[2] for r in rows}) > 1:
            hint = normalise_us_exchange(exchange_hint)
            rows = [r for r in rows if r[2] == hint]
            if len({r[2] for r in rows}) != 1:
                return None
        if not rows:
            return None
        sym, name, exchange, asset_class = rows[0]
        return Resolved(
            symbol=sym, name=name, exchange=exchange, asset_class=asset_class or "equity"
        )
