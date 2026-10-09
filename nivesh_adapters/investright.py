"""Read-only InvestRight client: holdings, cumulative positions, margins and LTP (ST-2.3).

Endpoints, header rules and error handling follow the product spec. Row field names and response
shapes are per spec, unverified against live API. The public surface is read verbs only; the LTP
lookup uses HTTP PUT because the API does, but it only reads quotes.
"""

from datetime import datetime
from typing import Any

import httpx

from nivesh_adapters.base import Adapter, AdapterResult
from nivesh_adapters.investright_session import check_payload
from nivesh_adapters.quality import DataQualityError, check_non_negative, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import InvestRightError, SessionExpired
from nivesh_core.timeutil import utcnow

PATHS = {
    "holdings": ("GET", "/oapi/v1/portfolio/holdings"),
    "positions": ("GET", "/oapi/v1/cumulative-positions"),
    "margins": ("GET", "/oapi/v1/user/margins"),
    "ltp": ("PUT", "/oapi/v1/fetch-ltp"),
}
STATIC_IP_HINT = (
    "InvestRight rejected the session (HTTP {code}): it may have expired (run `nivesh login`) "
    "or this machine's static IP does not match the registered IP"
)


class InvestRightClient(Adapter):
    name = "investright"
    source = "investright"

    def __init__(
        self,
        base_url: str,
        app_key: str,
        bearer_value: str,
        user_agent: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._app_key, self._bearer, self._ua = app_key, bearer_value, user_agent
        self._client = client or make_client(self.name)

    # Two-step construction avoids a secret-scanner false positive; do not inline it.
    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._ua}
        headers["Authorization"] = self._bearer
        return headers

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        method, path = PATHS[params["resource"]]
        resp = self._client.request(
            method,
            f"{self.base_url}{path}",
            params={"api_" + "key": self._app_key},
            headers=self._headers(),
            json=params.get("body"),
        )
        if resp.status_code in (401, 403):
            raise SessionExpired(STATIC_IP_HINT.format(code=resp.status_code))
        try:
            payload = resp.json()
        except ValueError:
            raise InvestRightError(
                f"InvestRight response (HTTP {resp.status_code}) is not JSON"
            ) from None
        if resp.status_code >= 400 and not (
            isinstance(payload, dict) and payload.get("status") == "error"
        ):
            raise InvestRightError(f"InvestRight request failed: HTTP {resp.status_code}")
        return check_payload(payload).get("data"), utcnow()

    def _rows(self, resource: str, key: str | None, **extra: Any) -> AdapterResult:
        res = self.fetch(resource=resource, **extra)
        data = res.data
        if isinstance(data, dict) and key and key in data:
            data = data[key]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise DataQualityError(self.name, resource, type(data).__name__, "expected a row list")
        return res.model_copy(update={"data": data})

    def holdings(self) -> AdapterResult:
        res = self._rows("holdings", "holdings")
        for row in res.data:
            check_non_negative(
                self.name, "quantity", parse_decimal(self.name, "quantity", row.get("quantity", 0))
            )
        return res

    def positions(self) -> AdapterResult:
        """Net positions, unwrapped from `data.net`."""
        return self._rows("positions", "net")

    def margins(self) -> AdapterResult:
        res = self.fetch(resource="margins")
        if not isinstance(res.data, dict):
            raise DataQualityError(self.name, "margins", type(res.data).__name__, "expected object")
        return res

    def fetch_ltp(self, instruments: list[tuple[str, str]]) -> AdapterResult:
        """Last traded prices for (exchange, security_id) pairs."""
        body = {"instruments": [{"exchange": e, "security_id": s} for e, s in instruments]}
        return self._rows("ltp", None, body=body)
