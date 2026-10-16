"""Harnesses of the fundamentals-provider contract suite (ADR-0016 D12).

A `Harness` is everything `tests/adapters/test_provider_contract.py` needs to hold one provider to
the contract without knowing its vendor: a mock double that serves recorded or hand-built data, a
way to build the provider on it, the figures the data must map to, one item that can be made
unavailable, and the vendor's hostile replies. To add a provider, write its harness here and append
it to `HARNESSES`; the contract suite then covers it (a provider in `BUILTIN` without a harness
fails `test_every_registered_provider_has_a_harness`).

The fake provider below doubles as the worked example: the smallest adapter + provider that passes.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.fundamentals_in import BseProvider
from nivesh_adapters.fundamentals_tapetide import TapetideProvider, scrub_text
from nivesh_adapters.fundamentals_upstox import (
    UpstoxFundamentals,
    UpstoxProvider,
    map_corporate_actions,
)
from nivesh_adapters.providers import (
    Capability,
    CollectContext,
    FundamentalsProvider,
    Ident,
    ProviderInfo,
    ProviderResult,
)
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import NotConfigured, RateLimited, SecretNotFound, SourceUnavailable
from nivesh_core.fundamentals_config import FundamentalsSettings, ProviderSpec
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.secrets import get_secret
from nivesh_core.timeutil import utcnow
from nivesh_engine.statements import StatementRow
from tests import tapetide_fx as tx
from tests.adapters.test_fundamentals_ingest import Bse, india

FX = Path(__file__).resolve().parent / "fixtures" / "market"
TODAY = date(2026, 10, 13)
CRORE, LAKH = Decimal(10) ** 7, Decimal(10) ** 5
HOSTILE = ("401", "echo", "malformed", "quota")
Key = tuple[str, str, date]  # (item, period type, period end)


@dataclass(frozen=True)
class Harness:
    name: str  # the registry name
    spec: ProviderSpec
    serve: Callable[..., Any]  # serve(drop=False, variant="") -> a fresh mock double
    adapter: Callable[[Any], Adapter]  # a double -> the vendor adapter on a MockTransport
    provider: Callable[[Any | None], FundamentalsProvider]  # a double (None: no transport)
    reply: Callable[[str], httpx.Response]  # the hostile reply of a kind in `HOSTILE`
    fetch_params: Mapping[str, Any]  # one adapter fetch the double serves
    known: Mapping[Key, Decimal]  # figures of the default variant, in absolute INR
    dropped: str  # the item `serve(drop=True)` makes unavailable (with a reason)
    early: date  # an as-of before the newest filing of the double
    cred: tuple[str, str] | None = None  # (secret name, dummy value); None: needs no credential
    variants: tuple[str, ...] = ("",)  # extra data sets that together show every capability
    # capabilities with no slot in `ProviderResult` (actions, estimates, news) are shown by a probe:
    # it takes a double and returns what the provider can produce beyond the result
    probe: Callable[[Any], set[Capability]] | None = None


# Tapetide (recorded envelopes) ------------------------------------------------------------------


def _tapetide() -> Harness:
    spec = FundamentalsSettings().provider("tapetide")
    assert spec is not None
    misc = tx.load("tapetide_misc.json")
    fy = date(2026, 3, 31)

    def provider(srv: Any) -> FundamentalsProvider:
        return TapetideProvider(spec, None if srv is None else lambda: tx.adapter(srv))

    def serve(drop: bool = False, variant: str = "") -> tx.Tapetide:
        srv = tx.Tapetide()
        if drop:  # cfo and capex come from the cash flow section: it fails, with a reason
            srv.fail_sections["cash_flow"] = httpx.Response(200, text="<html>bad gateway</html>")
        return srv

    def reply(kind: str) -> httpx.Response:
        text = {
            "echo": f"Error: invalid request for {tx.token()}",
            "quota": "Error: Rate limit exceeded: 400 requests per day",
        }
        if kind == "401":
            return httpx.Response(401, json=misc["err_bad_token_401"])
        if kind == "malformed":
            return httpx.Response(200, text="{not json")
        return httpx.Response(200, json=tx.envelope(text[kind], is_error=True))

    return Harness(
        name="tapetide",
        spec=spec,
        serve=serve,
        adapter=tx.adapter,
        provider=provider,
        reply=reply,
        fetch_params={"resource": "financials", "symbol": "RELIANCE", "section": "profit_loss"},
        known={
            ("revenue", "A", fy): Decimal(1055780) * CRORE,
            ("net_income", "A", fy): Decimal(95754) * CRORE,
            ("total_equity", "A", fy): Decimal(904030) * CRORE,
            ("eps", "A", fy): Decimal("59.69"),
            ("shares_out", "A", fy): Decimal(13532000000),
        },
        dropped="cfo",
        early=date(2025, 6, 30),
        cred=("TAPETIDE_TOKEN", tx.token()),
    )


# BSE XBRL (hand-built per spec, unverified) -------------------------------------------------------


def _bse() -> Harness:
    spec = FundamentalsSettings().provider("bse_xbrl")
    assert spec is not None
    end = date(2025, 12, 31)

    def provider(b: Any) -> FundamentalsProvider:
        return BseProvider(spec, None if b is None else india(b))

    def serve(drop: bool = False, variant: str = "") -> Bse:
        b = Bse()
        if variant == "bank":
            b.xml = (FX / "india_bank_results.xbrl").read_text()
        if drop:  # the results filing cannot be fetched: nothing of it exists, with a reason
            b.fail = {"r0.xml"}
        return b

    def reply(kind: str) -> httpx.Response:
        return {
            "401": httpx.Response(401),
            "echo": httpx.Response(200, text="<error>nothing to see</error>"),
            "malformed": httpx.Response(200, text="{not json"),
            "quota": httpx.Response(429),
        }[kind]

    return Harness(
        name="bse_xbrl",
        spec=spec,
        serve=serve,
        adapter=lambda b: india(b)(),
        provider=provider,
        reply=reply,
        fetch_params={"resource": "results_index", "scrip": "500325"},
        known={
            ("revenue", "Q", end): Decimal("2435.75") * LAKH,
            ("net_income", "Q", end): Decimal("305.55") * LAKH,
            ("eps", "Q", end): Decimal("22.61"),
            ("revenue", "A", date(2025, 3, 31)): Decimal("9300.10") * LAKH,
        },
        dropped="net_income",
        early=date(2025, 12, 31),
        variants=("", "bank"),
    )


# The fake provider: the smallest vendor that passes the contract ---------------------------------

FAKE_CRED = "FAKE_VENDOR_CRED"  # the name of its secret
FAKE_URL = "https://fake-vendor.invalid/v1/statements"
FAKE_MONEY = ("revenue", "net_income")  # in crore; eps is per share, shares_out a count


class FakeAdapter(Adapter):
    name = "fake_vendor"
    source = "fake"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        try:
            cred = get_secret(FAKE_CRED)
        except SecretNotFound:
            raise NotConfigured(f"{FAKE_CRED} not set; nivesh secrets set {FAKE_CRED}") from None
        resp = self._client.get(
            FAKE_URL, params={"symbol": params["symbol"]},
            headers={"Authorization": f"{'Bear' + 'er'} {cred}"},
        )  # fmt: skip
        if resp.status_code in (401, 403):
            raise SourceUnavailable("Fake vendor rejected the credential")
        if resp.status_code == 429:
            raise RateLimited("Fake vendor quota reached")
        if resp.status_code != 200:
            raise SourceUnavailable(f"Fake vendor returned HTTP {resp.status_code}")
        try:
            doc = resp.json()
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            raise SourceUnavailable("Fake vendor returned an unexpected response shape")
        if isinstance(doc.get("error"), str):  # an error text may echo the request: never the cred
            clean = scrub_text(doc["error"].replace(cred, ""))
            raise SourceUnavailable(f"Fake vendor error: {clean[:100]}")
        return doc, utcnow()


class FakeProvider(FundamentalsProvider):
    info = ProviderInfo(
        name="fake", label="Fake vendor", sources=("fake",),
        capabilities=frozenset({Capability.statements, Capability.shareholding, Capability.pledge}),
        keyed_on=Ident.nse_symbol, credential=FAKE_CRED, metered=False,
        revenue_basis="fake revenue, as the vendor reports it", profit_basis="fake net profit",
        filing_dates="declared",
    )  # fmt: skip

    def collect(self, ctx: CollectContext) -> ProviderResult:
        adapter = (self._make or FakeAdapter)()
        doc = ctx.get(adapter, {"resource": "statements", "symbol": ctx.sec.symbol}, "fundamentals",
                      ctx.sec.symbol, soft=True)  # fmt: skip
        res = ProviderResult()
        if doc is None:
            return res
        rows: list[StatementRow] = []
        for p in doc["periods"]:
            end, filed = date.fromisoformat(p["end"]), date.fromisoformat(p["filed"])
            if filed > ctx.today:
                continue  # not yet filed as of `today`
            for item, v in p["items"].items():
                if v is None:
                    res.skipped.append(f"{item} {end}: the vendor did not report it")
                    continue
                value = Decimal(str(v)) * (CRORE if item in FAKE_MONEY else 1)
                cur = "shares" if item == "shares_out" else "INR"
                rows.append(StatementRow(period_end=end, period_type=p["type"], item=item,
                                         value=value, currency=cur, filed_at=filed))  # fmt: skip
        res.groups.append(("fake", rows))
        for h in doc["holders"]:
            end, filed = date.fromisoformat(h["end"]), date.fromisoformat(h["filed"])
            if filed <= ctx.today:
                pledged = None if h["pledged"] is None else Decimal(str(h["pledged"]))
                res.shareholding.append(ShareholdingRow(
                    period_end=end, filed_at=filed, promoter_pct=Decimal(str(h["promoter"])),
                    promoter_pledged_pct=pledged, public_pct=Decimal(str(h["public"])),
                ))  # fmt: skip
        return res


def fake_doc(drop: bool = False) -> dict[str, Any]:
    items = {"revenue": 1000.5, "net_income": 120.25, "eps": 5.5, "shares_out": 21000000}
    return {
        "periods": [
            {"end": "2025-03-31", "type": "A", "filed": "2025-05-20",
             "items": {**items, "net_income": None if drop else 120.25}},
            {"end": "2025-12-31", "type": "Q", "filed": "2026-01-25",
             "items": {"revenue": 260, "net_income": None if drop else 31.5, "eps": 1.5}},
        ],
        "holders": [
            {"end": "2025-03-31", "filed": "2025-04-21", "promoter": 50.5, "public": 49.5,
             "pledged": None},
            {"end": "2025-12-31", "filed": "2026-01-20", "promoter": 50.25, "public": 49.75,
             "pledged": 1.25},
        ],
    }  # fmt: skip


class FakeVendor:
    """MockTransport handler of the fake vendor: 401 without the dummy credential."""

    def __init__(self, drop: bool = False) -> None:
        self.doc, self.calls = fake_doc(drop), 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        if req.headers.get("authorization") != f"{'Bear' + 'er'} {fake_cred()}":
            return httpx.Response(401)
        return httpx.Response(200, json=self.doc)


def fake_cred() -> str:
    return "fake" + "-" + "cred" + "-" + "42"


def _fake() -> Harness:
    spec = ProviderSpec(name="fake", credential=f"ref:{FAKE_CRED}")
    end = date(2025, 3, 31)

    def adapter(h: Any) -> FakeAdapter:
        return FakeAdapter(httpx.Client(transport=httpx.MockTransport(h)))

    def provider(h: Any) -> FundamentalsProvider:
        return FakeProvider(spec, None if h is None else lambda: adapter(h))

    def reply(kind: str) -> httpx.Response:
        return {
            "401": httpx.Response(401),
            "echo": httpx.Response(200, json={"error": f"bad request for {fake_cred()}"}),
            "malformed": httpx.Response(200, text="[1, 2"),
            "quota": httpx.Response(429),
        }[kind]

    return Harness(
        name="fake",
        spec=spec,
        serve=lambda drop=False, variant="": FakeVendor(drop),
        adapter=adapter,
        provider=provider,
        reply=reply,
        fetch_params={"resource": "statements", "symbol": "RELIANCE"},
        known={
            ("revenue", "A", end): Decimal("1000.5") * CRORE,
            ("net_income", "A", end): Decimal("120.25") * CRORE,
            ("eps", "A", end): Decimal("5.5"),
            ("shares_out", "A", end): Decimal(21000000),
        },
        dropped="net_income",
        early=date(2025, 6, 30),
        cred=(FAKE_CRED, fake_cred()),
    )


# Upstox (hand-written from the public API documentation, UNVERIFIED) -----------------------------

UPSTOX_CRED = "UPSTOX_ANALYTICS_TOKEN"  # noqa: S105 - the name of the secret, never a value
UPSTOX_ISIN = "INE002A01018"
UPSTOX_ENDPOINTS = {  # endpoint path -> fixture file
    "income-statement": "income_statement", "balance-sheet": "balance_sheet",
    "cash-flow": "cash_flow", "share-holdings": "share_holdings",
    "corporate-actions": "corporate_actions",
}  # fmt: skip


def upstox_cred() -> str:
    return "up" + "-" + "cred" + "-" + "42"


def upstox_doc(name: str) -> dict[str, Any]:
    """A hand-written fixture without its `_comment` (the reply a real endpoint would give)."""
    doc: dict[str, Any] = json.loads((FX / f"upstox_{name}.json").read_text())
    return {k: v for k, v in doc.items() if k != "_comment"}


class UpstoxVendor:
    """MockTransport handler of the Upstox fundamentals endpoints (shapes UNVERIFIED): 401 without
    the dummy credential, 400 for an ISIN it does not know, and `fail[endpoint]` for a chosen
    reply. Records every request."""

    def __init__(self, drop: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.docs = {path: upstox_doc(name) for path, name in UPSTOX_ENDPOINTS.items()}
        self.misc = upstox_doc("misc")
        self.fail: dict[str, httpx.Response] = {}
        if drop:  # the cash flow cannot be served: no operating cash flow exists, with a reason
            self.fail["cash-flow"] = httpx.Response(400, json=self.misc["err_invalid_type"])

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.headers.get("authorization") != f"{'Bear' + 'er'} {upstox_cred()}":
            return httpx.Response(401, json=self.misc["err_bad_token_401"])
        isin, endpoint = req.url.path.split("/")[-2:]
        if isin != UPSTOX_ISIN:
            return httpx.Response(400, json=self.misc["err_invalid_isin"])
        if endpoint in self.fail:
            return self.fail[endpoint]
        doc = self.docs[endpoint]
        if endpoint == "income-statement":
            doc = doc[req.url.params.get("time_period", "yearly")]
        return httpx.Response(200, json=doc)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def endpoints(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]


def upstox_adapter(vendor: Any) -> UpstoxFundamentals:
    return UpstoxFundamentals(httpx.Client(transport=httpx.MockTransport(vendor)))


def _upstox() -> Harness:
    spec = FundamentalsSettings().provider("upstox")
    assert spec is not None
    fy, qe = date(2026, 3, 31), date(2026, 6, 30)

    def provider(v: Any) -> FundamentalsProvider:
        return UpstoxProvider(spec, None if v is None else lambda: upstox_adapter(v))

    def probe(v: Any) -> set[Capability]:  # corporate actions are mapped, never ingested
        doc = upstox_adapter(v).fetch(resource="corporate_actions", isin=UPSTOX_ISIN).data
        return {Capability.actions} if map_corporate_actions(doc, 1)[0] else set()

    def reply(kind: str) -> httpx.Response:
        message = f"Invalid ISIN near {upstox_cred()}"
        echo = {"status": "error", "errors": [{"errorCode": "UDAPI1206", "message": message}]}
        return {
            "401": httpx.Response(401, json=upstox_doc("misc")["err_bad_token_401"]),
            "echo": httpx.Response(400, json=echo),
            "malformed": httpx.Response(200, text="{not json"),
            "quota": httpx.Response(429),
        }[kind]

    return Harness(
        name="upstox",
        spec=spec,
        serve=lambda drop=False, variant="": UpstoxVendor(drop),
        adapter=upstox_adapter,
        provider=provider,
        reply=reply,
        fetch_params={"resource": "income_statement", "isin": UPSTOX_ISIN, "time_period": "yearly"},
        known={
            ("revenue", "A", fy): Decimal("1350.75") * CRORE,
            ("net_income", "A", fy): Decimal("172.5") * CRORE,
            ("total_assets", "A", fy): Decimal("2400.75") * CRORE,
            ("total_equity", "A", fy): Decimal("1017.75") * CRORE,  # capital 67.5 + reserves 950.25
            ("cfo", "A", fy): Decimal("240.25") * CRORE,
            ("revenue", "Q", qe): Decimal("355.5") * CRORE,
        },
        dropped="cfo",
        early=date(2025, 7, 1),
        cred=(UPSTOX_CRED, upstox_cred()),
        probe=probe,
    )


# The harnesses the contract suite runs over. A new provider appends its own here.
HARNESSES: list[Harness] = [_tapetide(), _bse(), _fake(), _upstox()]
