"""Tapetide test double: a MockTransport handler serving the recorded (trimmed) envelopes.

The token is built by concatenation so no credential-shaped literal sits in the repo; the handler
checks the Authorization header the way the real endpoint does (401 without it).
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from nivesh_adapters.edgar import RateLimiter
from nivesh_adapters.fundamentals_tapetide import (
    TapetideFundamentals,
    face_value_of,
    map_shareholding,
    map_statements,
)
from nivesh_core.market_models import ShareholdingRow
from nivesh_engine.statements import StatementRow

FX = Path(__file__).resolve().parent / "fixtures" / "market"
SECTIONS = ("quarterly", "profit_loss", "balance_sheet", "cash_flow")


def token() -> str:
    return "tt" + "-" + "x" * 8 + "42"


def scheme() -> str:
    return "Bear" + "er"


def load(name: str) -> dict[str, Any]:
    return json.loads((FX / name).read_text())  # type: ignore[no-any-return]


def inner(envelope: dict[str, Any]) -> dict[str, Any]:
    return json.loads(envelope["result"]["content"][0]["text"])  # type: ignore[no-any-return]


def envelope(payload: dict[str, Any] | str, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": 1, "result": result}


class Tapetide:
    """Serves one fixture company per symbol; records every request body."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.misc = load("tapetide_misc.json")
        self.files = {s.upper(): load(f"tapetide_{s}.json") for s in
                      ("reliance", "hdfcbank", "tcs", "infy", "itc")}  # fmt: skip
        self.aliases: dict[str, str] = {}  # test symbol -> fixture company
        self.fail_sections: dict[str, httpx.Response] = {}
        self.fail_all: httpx.Response | None = None
        self.quota_after: int | None = None  # deny every request after this many

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        body = json.loads(req.content)
        self.bodies.append(body)
        if self.fail_all is not None:
            return self.fail_all
        if self.quota_after is not None and len(self.requests) > self.quota_after:
            text = "Error: Rate limit exceeded: 50 requests per day"
            return httpx.Response(200, json=envelope(text, is_error=True),
                                  headers={"Retry-After": "3600"})  # fmt: skip
        if req.headers.get("authorization") != f"{scheme()} {token()}":
            return httpx.Response(401, json=self.misc["err_bad_token_401"])
        args = body["params"]["arguments"]
        asked, tool = args["symbol"], body["params"]["name"]
        sym = self.aliases.get(asked, asked)
        if tool == "get_shareholding":
            return httpx.Response(200, json=self.misc["shareholding"])
        if sym not in self.files:
            return httpx.Response(200, json=self.misc["err_unknown_symbol"])
        section = args.get("section")
        if section is None:
            key = "snapshot_reliance" if sym == "RELIANCE" else f"synthetic_snapshot_{sym.lower()}"
            return httpx.Response(200, json=self.misc[key])
        if section in self.fail_sections:
            return self.fail_sections[section]
        return httpx.Response(200, json=self.files[sym][section])

    def edit(self, company: str, section: str, change: Callable[[dict[str, Any]], None]) -> None:
        """Change one recorded section in place (e.g. flip an availability basis)."""
        env = self.files[company][section]
        payload = inner(env)
        change(payload["data"][0])
        self.files[company][section] = envelope(payload)

    @property
    def calls(self) -> int:
        return len(self.requests)


def client(server: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(server))


def adapter(server: Any, sleeps: list[float] | None = None) -> TapetideFundamentals:
    """An adapter whose pacing runs on a fake clock (no real sleeping)."""
    log = sleeps if sleeps is not None else []
    limiter = RateLimiter(1 / 1.1, lambda: 0.0, log.append)
    return TapetideFundamentals(client(server), limiter=limiter)


TODAY = date(2026, 10, 13)


def recorded_sections(company: str) -> dict[str, Any]:
    return {k: inner(v)["data"][0] for k, v in load(f"tapetide_{company.lower()}.json").items()}


def recorded_face_value(company: str) -> Any:
    key = "snapshot_reliance" if company == "RELIANCE" else f"synthetic_snapshot_{company.lower()}"
    return face_value_of(inner(load("tapetide_misc.json")[key])["data"][0])


def recorded_rows(company: str, today: date = TODAY) -> list[StatementRow]:
    """The recorded company run through the real mapper (reported and estimated rows together)."""
    got = map_statements(recorded_sections(company), recorded_face_value(company), today)
    return got.reported + got.estimated


def recorded_shareholding(today: date = TODAY) -> list[ShareholdingRow]:
    return map_shareholding(inner(load("tapetide_misc.json")["shareholding"]), today).rows
