"""Mutual-fund scheme metadata and monthly portfolio holdings (ST-5.2, ST-5.3).

Wire formats are per spec, unverified against live sources. `_fetch` returns JSON-native data
(cacheable); `parse_*` build typed rows after the cache and are strict (`DataQualityError`).
Unknown TER or AUM is None, never zero. Plan and option come from the scheme name (a holding's
free-text plan is not trusted alone).
"""

import calendar
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.macro import secret_for
from nivesh_adapters.prices_in import UA, http_get
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import ConfigError, RateLimited, SourceUnavailable
from nivesh_core.mf_models import FundHoldingRow, FundMeta
from nivesh_core.secrets import REF_RE
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.mf_cost import option_of, plan_of

META_SOURCE = "mf_meta"
META_URL = "https://api.mfdata.example/v1/schemes/{code}"  # per spec, unverified; example host
MAX_TER_PCT = Decimal(5)  # a TER above this is a data error, not a fund

# target field -> accepted source keys (first present wins); the whole wire contract in one table
FIELD_MAP: dict[str, tuple[str, ...]] = {
    "amfi_code": ("amfi_code", "scheme_code"),
    "scheme_name": ("scheme_name",),
    "amc": ("amc", "fund_house"),
    "category": ("category", "scheme_category"),
    "expense_ratio": ("expense_ratio", "ter"),
    "aum_crore": ("aum_crore",),
    "benchmark": ("benchmark",),
    "manager": ("fund_manager", "manager"),
    "manager_since": ("manager_since",),
    "as_of": ("as_of",),
}


def _get(doc: dict[str, Any], target: str) -> Any:
    for key in FIELD_MAP[target]:
        value = doc.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _text(doc: dict[str, Any], target: str) -> str | None:
    v = _get(doc, target)
    return None if v is None else str(v).strip()


def _day(field: str, raw: object) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise DataQualityError(META_SOURCE, field, raw, "bad date") from None


def parse_meta(doc: Any, today: date | None = None) -> FundMeta:
    """One scheme's metadata; as_of defaults to the fetch date when the source omits it."""
    if not isinstance(doc, dict):
        raise DataQualityError(META_SOURCE, "document", type(doc).__name__, "expected object")
    limit = today or ist_date(utcnow())
    code, name = _text(doc, "amfi_code"), _text(doc, "scheme_name")
    if not code or not code.isdigit():
        raise DataQualityError(META_SOURCE, "amfi_code", code, "missing or not digits")
    if not name:
        raise DataQualityError(META_SOURCE, "scheme_name", name, "missing")
    raw_ter = _get(doc, "expense_ratio")
    ter = None if raw_ter is None else parse_decimal(META_SOURCE, "expense_ratio", raw_ter)
    if ter is not None and not (0 <= ter <= MAX_TER_PCT):
        raise DataQualityError(META_SOURCE, "expense_ratio", raw_ter, "outside 0 to 5 percent")
    raw_aum = _get(doc, "aum_crore")
    aum = None if raw_aum is None else parse_decimal(META_SOURCE, "aum_crore", raw_aum)
    if aum is not None and aum < 0:
        raise DataQualityError(META_SOURCE, "aum_crore", raw_aum, "must be >= 0")
    raw_since = _get(doc, "manager_since")
    as_of = _day("as_of", _get(doc, "as_of")) if _get(doc, "as_of") else limit
    if as_of > limit:
        raise DataQualityError(META_SOURCE, "as_of", as_of.isoformat(), "date is in the future")
    return FundMeta(
        as_of=as_of, amfi_code=code, scheme_name=name, amc=_text(doc, "amc"),
        category=_text(doc, "category"), plan=plan_of(name), option=option_of(name),
        expense_ratio=ter, aum_crore=aum, benchmark=_text(doc, "benchmark"),
        manager=_text(doc, "manager"),
        manager_since=None if raw_since is None else _day("manager_since", raw_since),
        source=META_SOURCE,
    )  # fmt: skip


class MfMetaClient(Adapter):
    name = "mf_meta"
    source = META_SOURCE

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        code = str(params["amfi_code"])
        if not code.isdigit():
            raise DataQualityError(self.name, "amfi_code", code, "must be digits")
        resp = http_get(self._client, META_URL.format(code=code))
        if resp is None:
            raise SourceUnavailable(f"metadata source has no scheme {code}")
        return resp.json(), utcnow()

    def validate(self, data: Any) -> None:
        parse_meta(data)


# ---- monthly portfolio holdings (ST-5.3) -------------------------------------------------------
HOLDINGS_SOURCE = "mf_holdings"
HOLDINGS_URL = "https://api.mfdata.example/v1/holdings/{code}"  # per spec, unverified; example host
MAX_TOTAL_PCT = Decimal(101)  # 100 plus rounding slack; more is a data error
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
SOURCES = ("mfdata", "amc", "fixture")


def _is_month_end(day: date) -> bool:
    return day.day == calendar.monthrange(day.year, day.month)[1]


def _line(
    month: date, isin: object, label: object, pct: object, kind_text: object
) -> FundHoldingRow:
    if kind_text is None or str(kind_text).strip() == "":
        raise DataQualityError(HOLDINGS_SOURCE, "asset_type", kind_text, "missing")
    kind: Literal["equity", "other"] = (
        "equity" if str(kind_text).strip().lower() == "equity" else "other"
    )
    weight = parse_decimal(HOLDINGS_SOURCE, "weight_pct", pct)
    if not (0 <= weight <= 100):
        raise DataQualityError(HOLDINGS_SOURCE, "weight_pct", pct, "outside 0 to 100")
    code = str(isin).strip().upper() if isin else ""
    if code and not _ISIN.match(code):
        raise DataQualityError(HOLDINGS_SOURCE, "isin", isin, "not an ISIN")
    if kind == "equity" and not code:
        raise DataQualityError(HOLDINGS_SOURCE, "isin", isin, "equity line without an ISIN")
    if not code:
        text = re.sub(r"[^A-Z0-9]+", "_", str(label or "").upper()).strip("_")
        if not text:
            raise DataQualityError(HOLDINGS_SOURCE, "label", label, "line has no ISIN or label")
        code = f"OTHER:{text}"
    return FundHoldingRow(
        month_end=month, isin=code, weight_pct=weight, kind=kind, source=HOLDINGS_SOURCE
    )


def _iso_month(raw: object) -> date:
    try:
        day = date.fromisoformat(str(raw))
    except ValueError:
        raise DataQualityError(HOLDINGS_SOURCE, "month_end", raw, "bad date") from None
    return day


def _dmy_month(raw: object) -> date:
    try:
        return datetime.strptime(str(raw), "%d-%m-%Y").date()  # noqa: DTZ007
    except ValueError:
        raise DataQualityError(HOLDINGS_SOURCE, "as_on", raw, "bad date") from None


def _grouped_nested(doc: dict[str, Any]) -> list[tuple[date, list[tuple[Any, ...]]]]:
    months = doc.get("months")
    if not isinstance(months, list):
        raise DataQualityError(HOLDINGS_SOURCE, "months", type(months).__name__, "expected list")
    out = []
    for m in months:
        try:
            lines = [
                (h.get("isin"), h.get("label"), h["weight_pct"], h.get("asset_type"))
                for h in m["holdings"]
            ]
            out.append((_iso_month(m["month_end"]), lines))
        except (KeyError, TypeError, AttributeError):
            raise DataQualityError(HOLDINGS_SOURCE, "months", m, "bad month entry") from None
    return out


def _grouped_flat(doc: dict[str, Any]) -> list[tuple[date, list[tuple[Any, ...]]]]:
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise DataQualityError(HOLDINGS_SOURCE, "rows", type(rows).__name__, "expected list")
    by_month: dict[date, list[tuple[Any, ...]]] = {}
    for r in rows:
        try:
            by_month.setdefault(_dmy_month(r["as_on"]), []).append(
                (r.get("isin"), r.get("label"), r["pct"], r.get("type"))
            )
        except (KeyError, TypeError, AttributeError):
            raise DataQualityError(HOLDINGS_SOURCE, "rows", r, "bad row") from None
    return sorted(by_month.items())


# holdings_source -> (key naming the scheme code, grouping function): the whole wire contract
_SHAPES: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {
    "mfdata": ("amfi_code", _grouped_nested),
    "fixture": ("amfi_code", _grouped_nested),
    "amc": ("scheme", _grouped_flat),
}


def parse_holdings(doc: Any, source: str, amfi_code: str) -> dict[date, list[FundHoldingRow]]:
    """Month end -> portfolio lines. Strict: bad ISIN, weight, month end or a month summing to
    more than 101 percent raises. Cash, derivative and debt lines are `other`, never dropped."""
    if source not in _SHAPES:
        raise DataQualityError(HOLDINGS_SOURCE, "source", source, "unknown holdings source")
    if not isinstance(doc, dict):
        raise DataQualityError(HOLDINGS_SOURCE, "document", type(doc).__name__, "expected object")
    code_key, group = _SHAPES[source]
    if str(doc.get(code_key, "")) != amfi_code:
        raise DataQualityError(HOLDINGS_SOURCE, "amfi_code", doc.get(code_key), "scheme mismatch")
    out: dict[date, list[FundHoldingRow]] = {}
    for month, lines in group(doc):
        if not _is_month_end(month):
            raise DataQualityError(HOLDINGS_SOURCE, "month_end", month.isoformat(), "not month end")
        rows = [_line(month, *line) for line in lines]
        seen = [r.isin for r in rows]
        if len(set(seen)) != len(seen) or month in out:
            raise DataQualityError(HOLDINGS_SOURCE, "isin", month.isoformat(), "duplicate line")
        if sum((r.weight_pct for r in rows), Decimal(0)) > MAX_TOTAL_PCT:
            raise DataQualityError(
                HOLDINGS_SOURCE, "weight_pct", month.isoformat(), "weights sum to more than 101"
            )
        out[month] = rows
    return out


class MfHoldingsClient(Adapter):
    """Monthly portfolio source. The credential, when the source needs one, is a `ref:NAME`
    resolved at request time and sent only as a header: never in params, cache or logs."""

    name = "mf_holdings"
    source = HOLDINGS_SOURCE
    _KEY_HEADER = "X-Api-" + "Key"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        source: str = "fixture",
        key_ref: str = "ref:MFDATA_API_KEY",
    ) -> None:
        if not REF_RE.match(key_ref):
            raise ConfigError("holdings credential must be a reference such as 'ref:NAME'")
        if source not in SOURCES:
            raise ConfigError(f"unknown holdings source {source!r}; expected one of {SOURCES}")
        self._client = client or make_client(self.name)
        self._holdings_source, self._key_ref = source, key_ref

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        code = str(params["amfi_code"])
        if not code.isdigit():
            raise DataQualityError(self.name, "amfi_code", code, "must be digits")
        headers = dict(UA)
        if self._holdings_source == "mfdata":
            headers[self._KEY_HEADER] = secret_for(self._key_ref)
        url = HOLDINGS_URL.format(code=code)
        resp = self._client.send(self._client.build_request("GET", url, headers=headers))
        host = httpx.URL(url).host
        if resp.status_code == 429:
            raise RateLimited(f"{host} rate-limited the request")
        if resp.status_code != 200:
            raise SourceUnavailable(f"{host} returned HTTP {resp.status_code} for scheme {code}")
        return resp.json(), utcnow()

    def validate(self, data: Any) -> None:
        code_key, _ = _SHAPES[self._holdings_source]
        own = str(data.get(code_key, "")) if isinstance(data, dict) else ""
        parse_holdings(data, self._holdings_source, own)
