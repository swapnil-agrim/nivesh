"""India fundamentals from the Upstox Company Fundamentals API (ADR-0016): a DISABLED skeleton.

EVERY wire format here is UNVERIFIED. The paths, query parameters, error codes and field names come
from a summarised read of Upstox's public API documentation; where the documentation was silent the
shape is a guess. No account was used and no call has ever been made to the real service. The
fixtures (`tests/fixtures/market/upstox_*.json`) are hand-written, and the first real response is
expected to need mapper edits. Enabling it takes two owner decisions: `enabled: true` for `upstox`
under `fundamentals.providers`, and an Analytics Token set with
`nivesh secrets set UPSTOX_ANALYTICS_TOKEN`.

What the documented API does not give: a filing date (every `filed_at` here is an estimate from the
period end), a pledge, a public holding (`retail_and_other` is not Public), a minority-interest
line. Corporate actions are mapped but NOT ingested (no wiring in this goal). The adapter only
reads (GET); the token is read per call through the secret store and lives in no attribute, cache
parameter or message. `_fetch` returns JSON-native data; the pure mappers turn it into stored rows
after the cache.
"""

import json
import math
import re
import sqlite3
from collections.abc import Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.edgar import RateLimiter
from nivesh_adapters.fundamentals_tapetide import CRORE, _dec, period_end, scrub_text
from nivesh_adapters.providers import (
    MAPPING_ERRORS,
    Capability,
    CollectContext,
    FundamentalsProvider,
    Ident,
    ProviderInfo,
    ProviderResult,
    dedupe_tail,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import (
    MarketDataError,
    NotConfigured,
    QuotaExceeded,
    RateLimited,
    SecretNotFound,
    SourceUnavailable,
)
from nivesh_core.market_models import CorpAction, ShareholdingRow
from nivesh_core.quota import Budget, Outcome
from nivesh_core.secrets import get_secret
from nivesh_core.timeutil import utcnow
from nivesh_engine.statements import PeriodType, StatementRow

SOURCE = "upstox"
SECRET_NAME = "UPSTOX_ANALYTICS_TOKEN"  # noqa: S105 - the name of the secret, never a value
BASE = "https://api.upstox.com/v2"
TIMEOUT_S = 30.0
MIN_INTERVAL_S = 0.0  # no rate limit is documented: a plan's `min_interval_s` paces the calls
MAX_CELL = 1e12  # statement cells are INR crore: 1e12 crore x 1e7 still fits DECIMAL(28,4)
# The documented replies carry no filing date, so each is estimated from the period end. The lags
# are the usual regulatory deadlines, approximate (ponytail: one constant per kind, no per-company
# history; replace them with the real dates if a verified response ever carries them).
QUARTERLY_LAG = timedelta(days=45)
ANNUAL_LAG = timedelta(days=60)
SHAREHOLDING_LAG = timedelta(days=21)
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
ENDPOINTS: dict[str, tuple[str, dict[str, str]]] = {  # resource -> (path, fixed query)
    "income_statement": ("income-statement", {"type": "consolidated"}),
    "balance_sheet": ("balance-sheet", {"type": "consolidated", "fs": "true"}),
    "cash_flow": ("cash-flow", {"type": "consolidated", "fs": "true"}),
    "share_holdings": ("share-holdings", {}),
    "corporate_actions": ("corporate-actions", {}),
}
BASIS = "consolidated unless the fixture says otherwise (UNVERIFIED)"


def _shape() -> SourceUnavailable:
    return SourceUnavailable("Upstox returned an unexpected response shape")


def _error_text(body: Any, cred: str) -> str | None:
    """'CODE: message' of the first entry of a documented `errors` list, scrubbed and cut; None
    when the body carries no error code."""
    errors = body.get("errors") if isinstance(body, dict) else None
    first = errors[0] if isinstance(errors, list) and errors else None
    if not isinstance(first, dict) or not first.get("errorCode"):
        return None
    text = f"{first['errorCode']}: {first.get('message', '')}".replace(cred, "")
    return scrub_text(" ".join(text.split()))[:120]


class UpstoxFundamentals(Adapter):
    name = "fundamentals_upstox"
    source = SOURCE

    def __init__(
        self, client: httpx.Client | None = None, *, limiter: RateLimiter | None = None
    ) -> None:
        # client and limiter are built after the token check: no secret, no network object
        self._client = client
        self._limiter = limiter
        self._secret = SECRET_NAME  # the provider sets both from its spec (`configure`)
        self._min_interval_s = MIN_INTERVAL_S
        self._budget: Budget | None = None
        self._halted: tuple[type[MarketDataError], str] | None = None

    def configure(self, *, secret_name: str, min_interval_s: float) -> None:
        """The name of the secret to read and the pacing (used unless a limiter was injected)."""
        self._secret, self._min_interval_s = secret_name, min_interval_s

    def use_budget(self, budget: Budget | None) -> None:
        """Gate every request on a quota ledger (a spec with a plan); none: no gate."""
        self._budget = budget

    def _halt(self, kind: type[MarketDataError], reason: str) -> MarketDataError:
        self._halted = (kind, reason)  # no retry that could repeat a rejected or limited request
        return kind(reason)

    def _cred(self) -> str:
        try:
            cred = "".join(get_secret(self._secret).split())  # a pasted newline is not part of it
        except SecretNotFound:
            cred = ""
        if not cred:
            raise self._halt(
                NotConfigured,
                f"{self._secret} not set; create an Analytics Token in the Upstox Developer Apps "
                f"console, then: nivesh secrets set {self._secret}",
            )
        return cred

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource, isin = params["resource"], params["isin"]
        if resource not in ENDPOINTS:
            raise ValueError(f"unknown resource {resource!r}")
        if not isinstance(isin, str) or not _ISIN.fullmatch(isin):  # it goes into the URL path
            raise ValueError("expected an ISIN")
        query = dict(ENDPOINTS[resource][1])
        if "time_period" in params:
            if params["time_period"] not in ("yearly", "quarterly"):
                raise ValueError("time_period must be yearly or quarterly")
            query["time_period"] = params["time_period"]
        if self._halted is not None:
            raise self._halted[0](self._halted[1])
        cred = self._cred()
        try:
            return self._get(cred, resource, isin, query), utcnow()
        except MarketDataError:
            raise
        except Exception as e:  # noqa: BLE001 - any text of it could hold the token
            raise SourceUnavailable(f"Upstox request failed ({type(e).__name__})") from None

    def _get(self, cred: str, resource: str, isin: str, query: dict[str, str]) -> dict[str, Any]:
        if self._client is None:
            self._client = make_client(self.name)
        if self._limiter is None and self._min_interval_s > 0:
            self._limiter = RateLimiter(1 / self._min_interval_s)
        if self._limiter is not None:
            self._limiter.wait()
        call_id = self._reserve(resource, isin)
        ok = False
        try:
            body = self._send(self._client, cred, resource, isin, query)
            ok = True
            return body
        finally:
            if self._budget is not None and call_id is not None:
                outcome: Outcome = "ok" if ok else "halted" if self._halted else "error"
                with suppress(sqlite3.Error):  # counted already; the outcome is advisory
                    self._budget.settle(call_id, outcome)

    def _reserve(self, resource: str, isin: str) -> int | None:
        if self._budget is None:
            return None
        try:  # the one chokepoint: no request leaves without a committed ledger row
            return self._budget.reserve(resource, isin)
        except QuotaExceeded as e:
            raise self._halt(QuotaExceeded, str(e)) from None
        except sqlite3.Error:  # a locked or broken ledger cannot count, so nothing may be spent
            raise self._halt(SourceUnavailable, "quota ledger unavailable") from None

    def _send(
        self, client: httpx.Client, cred: str, resource: str, isin: str, query: dict[str, str]
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"{'Bear' + 'er'} {cred}",  # the scheme is UNVERIFIED (not documented)
            "Accept": "application/json",
            "User-Agent": "nivesh/0.1",
        }
        url = f"{BASE}/fundamentals/{isin}/{ENDPOINTS[resource][0]}"
        resp = client.get(url, params=query, headers=headers, timeout=TIMEOUT_S)
        if resp.status_code in (401, 403):
            raise self._halt(
                SourceUnavailable,
                f"Upstox rejected the token (HTTP {resp.status_code}): create a new Analytics "
                f"Token in the Upstox Developer Apps console and run `nivesh secrets set "
                f"{self._secret}`",
            )
        if resp.status_code == 429:  # no quota is documented, so none is persisted: just stop
            raise self._halt(RateLimited, "Upstox rate-limited the request; try again later")
        try:
            body = json.loads(resp.text)
        except (ValueError, RecursionError):  # a bracket bomb overflows the parser's stack
            body = None
        if (error := _error_text(body, cred)) is not None:  # documented codes, whatever the status
            raise SourceUnavailable(f"Upstox error {error}")
        if resp.status_code != 200:
            raise SourceUnavailable(f"Upstox returned HTTP {resp.status_code}")
        if (
            not isinstance(body, dict)
            or body.get("status") != "success"
            or not isinstance(body.get("data"), dict)
        ):
            raise _shape()
        return body

    def validate(self, data: Any) -> None:
        """Every number is finite and bounded, so no vendor value can overflow a stored column.
        Iterative: a deeply nested document cannot overflow the stack."""
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, int | float) and not isinstance(node, bool):
                if isinstance(node, float) and not math.isfinite(node):
                    raise DataQualityError(SOURCE, "cell", node, "expected a finite number")
                if abs(node) > MAX_CELL:
                    raise DataQualityError(SOURCE, "cell", node, f"magnitude above {MAX_CELL:g}")


@dataclass
class Mapped:
    statements: list[StatementRow] = field(default_factory=list)
    shareholding: list[ShareholdingRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    clamped: int = 0  # filing dates moved back to today


def _data(doc: Any) -> dict[str, Any]:
    data = doc.get("data") if isinstance(doc, dict) else None
    return data if isinstance(data, dict) else {}


def _dicts(v: Any) -> list[dict[str, Any]]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _column(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    """{period label: value} of one field of the `history` rows ({period, <field>, ...})."""
    return {r["period"]: r.get(name) for r in _dicts(data.get("history"))
            if isinstance(r.get("period"), str)}  # fmt: skip


def _series(entries: Any, key: str, name: str) -> dict[str, Any]:
    """{period label: value} of the entry whose `key` is `name`, in a list of
    {<key>, history: [{period, value}]} entries (statement lines, holder categories)."""
    for entry in _dicts(entries):
        if entry.get(key) == name:
            return {h["period"]: h.get("value") for h in _dicts(entry.get("history"))
                    if isinstance(h.get("period"), str)}  # fmt: skip
    return {}


def _num(raw: Any) -> Decimal | None:
    return raw if isinstance(raw, Decimal) else _dec(raw)


def _income(data: Mapping[str, Any], skipped: list[str]) -> dict[str, dict[str, Any]]:
    return {"revenue": _column(data, "revenue"), "net_income": _column(data, "net_profit")}


def _cash_flow(data: Mapping[str, Any], skipped: list[str]) -> dict[str, dict[str, Any]]:
    return {"cfo": _series(data.get("categories"), "category", "operating")}


def _balance(data: Mapping[str, Any], skipped: list[str]) -> dict[str, dict[str, Any]]:
    """Total equity is share capital plus reserves (as Tapetide defines it), so it exists for a
    period only when BOTH lines of the full statement do; "equity capital" alone is not equity."""
    lines = data.get("full_statement")
    capital, reserves = (_series(lines, "name", n) for n in ("Equity Capital", "Reserves"))
    equity: dict[str, Any] = {}
    missing = 0
    for label in {*capital, *reserves}:
        c, r = _dec(capital.get(label)), _dec(reserves.get(label))
        if c is None or r is None:
            missing += 1
        else:
            equity[label] = c + r
    if missing:
        skipped.append(f"total_equity: Equity Capital and Reserves are both needed, missing or "
                       f"not a number for {missing} period(s)")  # fmt: skip
    elif not equity:
        skipped.append("total_equity: no Equity Capital and Reserves lines in the balance sheet, "
                       "none stored")  # fmt: skip
    return {"total_assets": _column(data, "total_asset"), "total_equity": equity}


# reply key (see UpstoxProvider.collect), period type, reader of the reply's data
_SECTIONS: tuple[tuple[str, PeriodType, Any], ...] = (
    ("income_yearly", "A", _income),
    ("income_quarterly", "Q", _income),
    ("balance_sheet", "A", _balance),
    ("cash_flow", "A", _cash_flow),
)


def _periods(name: str, labels: Collection[str], today: date, out: Mapped) -> dict[str, date]:
    """label -> period end for the labels that are understood and have ended. The others are
    listed once each ('TTM' is the vendor's trailing pseudo-period and is ignored)."""
    ends = {lb: e for lb in labels if (e := period_end(lb)) is not None}
    unknown = sorted(set(labels) - ends.keys() - {"TTM"})
    future = sorted((lb for lb, e in ends.items() if e > today), key=lambda lb: ends[lb])
    if unknown:
        out.skipped.append(f"{name}: {len(unknown)} period label(s) not understood "
                           f"({', '.join(unknown[:5])})")  # fmt: skip
    if future:
        out.skipped.append(f"{name}: {len(future)} period(s) end after today, skipped "
                           f"({', '.join(future[:5])})")  # fmt: skip
    return {lb: e for lb, e in ends.items() if e <= today}


def map_statements(docs: Mapping[str, Any], today: date) -> Mapped:
    """Upstox statement replies (keyed income_yearly, income_quarterly, balance_sheet, cash_flow)
    -> stored rows in absolute INR. Units must be crore (anything else is listed, not guessed);
    a null cell is absent, a cell that is not a number is listed; every filing date is an estimate
    and never after `today`. Pure: no I/O."""
    out = Mapped()
    for name, ptype, reader in _SECTIONS:
        if name not in docs:
            continue
        data = _data(docs[name])
        if str(data.get("units_in", "")).lower() != "crore":
            units = str(data.get("units_in"))[:20]
            out.skipped.append(f"{name}: units are {units!r}, not crore, so nothing stored")
            continue
        items: dict[str, dict[str, Any]] = reader(data, out.skipped)
        ends = _periods(name, {lb for cells in items.values() for lb in cells}, today, out)
        lag = ANNUAL_LAG if ptype == "A" else QUARTERLY_LAG
        for item, cells in items.items():
            for label, raw in cells.items():
                if label not in ends or raw is None:
                    continue
                value = _num(raw)
                if value is None:
                    out.skipped.append(f"{item} {label}: not a number in {name}, none stored")
                    continue
                end, filed = ends[label], min(ends[label] + lag, today)
                out.clamped += filed < end + lag
                out.statements.append(StatementRow(
                    period_end=end, period_type=ptype, item=item, value=value * CRORE,
                    currency="INR", filed_at=filed,
                ))  # fmt: skip
    return out


def map_shareholding(doc: Any, today: date) -> Mapped:
    """The promoter percentage per quarter. The documented categories give no pledge, no filing
    date, and nothing that is the vendor-neutral Public holding (`retail_and_other` is not), so
    only `promoters` is used."""
    out = Mapped()
    cells = _series(_data(doc).get("categories"), "category", "promoters")
    ends = _periods("shareholding", cells.keys(), today, out)
    for label, end in sorted(ends.items(), key=lambda kv: kv[1]):
        if cells[label] is None:
            continue
        pct = _dec(cells[label])
        if pct is None or not 0 <= pct <= 100:
            out.skipped.append(f"shareholding {label}: promoter percentage is not a number "
                               "between 0 and 100, none stored")  # fmt: skip
            continue
        filed = min(end + SHAREHOLDING_LAG, today)
        out.clamped += filed < end + SHAREHOLDING_LAG
        out.shareholding.append(ShareholdingRow(period_end=end, filed_at=filed, promoter_pct=pct))
    return out


_KINDS: dict[str, Literal["split", "bonus", "dividend"]] = {
    "Dividend": "dividend", "Bonus": "bonus", "Split": "split",
}  # fmt: skip
_RATIO = re.compile(r"(\d+)\s*:\s*(\d+)")


def _day(text: Any) -> date | None:
    try:
        return datetime.strptime(str(text), "%d %b %Y").date()  # "14 Aug 2025"
    except ValueError:
        return None


def map_corporate_actions(doc: Any, security_id: int) -> tuple[list[CorpAction], list[str]]:
    """Dividend, bonus and split actions (the ex-date is read from `expiry_date`; a ratio 'a:b' is
    read as a/b). UNVERIFIED, in particular the direction of a split ratio: nothing may rely on
    it before a live response has been seen. Rights issues and anything unusable are listed as
    skipped. NOT called by the ingest: the capability is declared, the wiring is a follow-up."""
    actions: list[CorpAction] = []
    skipped: list[str] = []
    raw = _data(doc).get("corporate_actions")
    for item in raw if isinstance(raw, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        kind = _KINDS.get(name) if isinstance(name, str) else None
        if kind is None or not isinstance(item, dict):
            skipped.append(f"corporate action {str(name)[:20]!r}: not a stored kind")
            continue
        ex = _day(item.get("expiry_date"))
        amount = _dec(item.get("amount")) if kind == "dividend" else None
        m = None if kind == "dividend" else _RATIO.fullmatch(str(item.get("ratio")).strip())
        ratio = Decimal(m[1]) / Decimal(m[2]) if m and int(m[2]) else None
        if (
            ex is None
            or (kind == "dividend" and not (amount and amount > 0))
            or (kind != "dividend" and ratio is None)
        ):
            skipped.append(f"{name} {str(item.get('expiry_date'))[:20]}: unusable date, amount or "
                           "ratio, none stored")  # fmt: skip
            continue
        actions.append(CorpAction(security_id=security_id, ex_date=ex, kind=kind, ratio=ratio,
                                  amount=amount, source=SOURCE))  # fmt: skip
    return sorted(actions, key=lambda a: a.ex_date), skipped


# reply key -> adapter parameters (income statements are asked yearly and quarterly)
REQUESTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("income_yearly", {"resource": "income_statement", "time_period": "yearly"}),
    ("income_quarterly", {"resource": "income_statement", "time_period": "quarterly"}),
    ("balance_sheet", {"resource": "balance_sheet"}),
    ("cash_flow", {"resource": "cash_flow"}),
)


class UpstoxProvider(FundamentalsProvider):
    """Upstox (disabled by default), keyed on the ISIN: statements, the promoter holding and, as a
    capability only, corporate actions. All wire formats UNVERIFIED; every filing date estimated."""

    info = ProviderInfo(
        name="upstox", label="Upstox", sources=(SOURCE,),
        capabilities=frozenset(
            {Capability.statements, Capability.shareholding, Capability.actions}
        ),
        keyed_on=Ident.isin, credential=SECRET_NAME, metered=False,
        revenue_basis=f"vendor revenue, {BASIS}", profit_basis=f"vendor net profit, {BASIS}",
        filing_dates="estimated",
    )  # fmt: skip

    def collect(self, ctx: CollectContext) -> ProviderResult:
        sym, isin = ctx.sec.symbol, ctx.sec.isin
        if not isin or not _ISIN.fullmatch(isin):  # never guess one: it would fetch another firm
            raise NotConfigured(f"{sym} has no valid ISIN in the security master and Upstox is "
                                "keyed on ISIN; run `nivesh master build`")  # fmt: skip
        up: UpstoxFundamentals = (self._make or UpstoxFundamentals)()
        up.use_budget(ctx.budget)  # a spec with a plan gates the requests; without one, none
        plan = self.spec.plan
        up.configure(
            secret_name=self.credential_name or SECRET_NAME,
            min_interval_s=plan.min_interval_s if plan else MIN_INTERVAL_S,
        )
        start = len(ctx.failed)
        docs: dict[str, Any] = {}
        for name, params in REQUESTS:
            doc = ctx.get(up, {**params, "isin": isin}, "fundamentals", f"{sym} {name}", soft=True)
            if doc is not None:
                docs[name] = doc
        holding = ctx.get(up, {"resource": "share_holdings", "isin": isin}, "fundamentals",
                          f"{sym} share_holdings", soft=True)  # fmt: skip
        dedupe_tail(ctx.failed, start)  # a halted source repeats its reason for every request
        mapped, held = Mapped(), Mapped()
        try:
            mapped = map_statements(docs, ctx.today)
        except MAPPING_ERRORS as e:
            ctx.failed.append(f"fundamentals {sym} statements: unusable Upstox data ({e})"[:200])
        if holding is not None:
            try:
                held = map_shareholding(holding, ctx.today)
            except MAPPING_ERRORS as e:
                msg = f"fundamentals {sym} shareholding: unusable Upstox data ({e})"
                ctx.failed.append(msg[:200])
        res = ProviderResult(
            groups=[(SOURCE, mapped.statements)], shareholding=held.shareholding,
            skipped=[*mapped.skipped, *held.skipped], clamped=mapped.clamped + held.clamped,
            estimated=len(mapped.statements),
        )  # fmt: skip
        if res.estimated:
            res.notes.append(
                f"{res.estimated} of {res.estimated} rows carry an estimated filing date (Upstox "
                f"gives none: statements +{QUARTERLY_LAG.days} days quarterly and "
                f"+{ANNUAL_LAG.days} days annual, shareholding +{SHAREHOLDING_LAG.days} days; "
                "approximate)"
            )
        if res.clamped:
            res.notes.append(f"{res.clamped} filing dates after today were set to today")
        return res
