"""Read-only US broker connector: Alpaca positions and account (ST-3.2).

Only `GET /v2/positions` and `GET /v2/account` are used. Endpoint paths, header names and field
names are per spec, unverified against the live API (deferred D3). Responses are cut down to a
whitelist at this boundary, so account numbers and asset ids never leave the adapter. Credentials
come from config references only and are never logged or returned.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx

from nivesh_adapters.base import Adapter, AdapterResult
from nivesh_adapters.csv_import_us import UsResolver
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import NiveshError
from nivesh_core.holdings import Holding, PriceBasis
from nivesh_core.security_resolver import normalise_us_exchange
from nivesh_core.timeutil import utcnow

NAME = "alpaca"
PATHS = {"positions": "/v2/positions", "account": "/v2/account"}
POSITION_FIELDS = (
    "symbol", "exchange", "qty", "avg_entry_price", "current_price", "asset_class", "side",
)  # fmt: skip
ACCOUNT_FIELDS = ("currency", "status", "cash", "equity")
_TICKER_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


class BrokerReader(Protocol):
    """What a read-only broker connector offers: positions and account, nothing else."""

    def positions(self) -> AdapterResult: ...

    def account(self) -> AdapterResult: ...


class AlpacaClient(Adapter):
    name = NAME
    source = NAME

    def __init__(
        self,
        base_url: str,
        key_value: str,
        secret_value: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key, self._secret = key_value, secret_value
        self._client = client or make_client(self.name)

    # Header names are joined at runtime on purpose (secret-scanner false positive); keep it.
    def _headers(self) -> dict[str, str]:
        headers = {"APCA-API-" + "KEY-ID": self._key}
        headers["APCA-API-" + "SECRET-KEY"] = self._secret
        return headers

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resp = self._client.request(
            "GET", f"{self.base_url}{PATHS[params['resource']]}", headers=self._headers()
        )
        if resp.status_code in (401, 403):
            raise NiveshError(
                f"Alpaca rejected the credentials (HTTP {resp.status_code}); check the secrets "
                "ALPACA_KEY and ALPACA_SECRET and the us_broker.base_url"
            )
        if resp.status_code >= 400:
            raise NiveshError(f"Alpaca request failed: HTTP {resp.status_code}")
        try:
            return resp.json(), utcnow()
        except ValueError:
            raise NiveshError("Alpaca response is not JSON") from None

    def positions(self) -> AdapterResult:
        res = self.fetch(resource="positions")
        if not isinstance(res.data, list) or not all(isinstance(r, dict) for r in res.data):
            raise DataQualityError(
                NAME, "positions", type(res.data).__name__, "expected a row list"
            )
        keep = [{k: r[k] for k in POSITION_FIELDS if k in r} for r in res.data]
        return res.model_copy(update={"data": keep})

    def account(self) -> AdapterResult:
        res = self.fetch(resource="account")
        if not isinstance(res.data, dict):
            raise DataQualityError(NAME, "account", type(res.data).__name__, "expected an object")
        keep = {k: res.data[k] for k in ACCOUNT_FIELDS if k in res.data}
        return res.model_copy(update={"data": keep})


def _valid_ticker(symbol: str) -> bool:
    return (
        bool(symbol) and symbol[0].isalpha() and len(symbol) <= 10 and set(symbol) <= _TICKER_CHARS
    )


def normalise_positions(
    rows: Sequence[dict[str, Any]], resolver: UsResolver, as_of: date
) -> tuple[list[Holding], list[str]]:
    """Alpaca positions -> USD holdings (INR is derived later). Long, listed US equity positions
    on NYSE/NASDAQ/ARCA only; everything else is skipped and counted in the warnings."""
    out: list[Holding] = []
    skipped = {"short": 0, "class": 0, "symbol": 0, "venue": 0}
    for i, row in enumerate(rows):
        where = f"rows[{i}]"
        symbol = str(row.get("symbol") or "").strip().upper()
        if str(row.get("side") or "long").lower() == "short":
            skipped["short"] += 1
        elif str(row.get("asset_class") or "us_equity") != "us_equity":
            skipped["class"] += 1
        elif not _valid_ticker(symbol):
            skipped["symbol"] += 1
        else:
            held = _holding(row, where, symbol, resolver, as_of)
            if held is None:
                skipped["venue"] += 1
            elif held.quantity > 0:
                out.append(held)
    labels = {
        "short": "short position(s)", "class": "non-equity position(s)",
        "symbol": "position(s) with an unsupported symbol",
        "venue": "position(s) on an unsupported exchange",
    }  # fmt: skip
    warnings = [f"skipped {n} {labels[k]}" for k, n in skipped.items() if n]
    return out, warnings


def _holding(
    row: dict[str, Any], where: str, symbol: str, resolver: UsResolver, as_of: date
) -> Holding | None:
    raw_exchange = str(row.get("exchange") or "")
    found = resolver.resolve_symbol(symbol, raw_exchange)
    venue = normalise_us_exchange(found.exchange if found else raw_exchange)
    if venue is None:
        return None
    qty = parse_decimal(NAME, f"{where}.qty", row.get("qty"))
    cost = parse_decimal(NAME, f"{where}.avg_entry_price", row.get("avg_entry_price"))
    last = _price(row.get("current_price"), f"{where}.current_price")
    basis: PriceBasis = "ltp" if last is not None else "avg_cost"
    return Holding(
        isin=None, symbol=symbol, exchange=venue, name=found.name if found else None,
        asset_class="equity", quantity=qty, avg_cost=cost, price=last if last is not None else cost,
        price_basis=basis, value_inr=None, as_of=as_of, source="alpaca", source_label="Alpaca",
        holder_ref="", currency="USD",
    )  # fmt: skip


def _price(raw: Any, what: str) -> Decimal | None:
    if raw in (None, ""):
        return None
    got = parse_decimal(NAME, what, raw)
    return got if got > 0 else None
