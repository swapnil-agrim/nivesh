"""SEC EDGAR (ST-4.5): company facts, the filing list and 10-K/10-Q/8-K sections by name.

Every request sends the contact User-Agent (`nivesh <EDGAR_CONTACT secret>`; the contact is PII
and lives only in the secret store) and passes a `RateLimiter` below SEC's 10 requests/second.
Wire formats are per spec, unverified against live sources (follow-up D2). `_fetch` returns
JSON-native data; a filing document is reduced to its named sections before it is cached, so
large HTML bodies never land in `cache_entry`.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.master_sources import sec_headers
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import NiveshError, RateLimited, SourceUnavailable
from nivesh_core.security_master import SecurityMaster
from nivesh_core.timeutil import utcnow
from nivesh_engine.statements import QuarterisedFacts, facts_from_companyfacts, quarterise

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FORMS = ("10-K", "10-Q", "8-K")
MAX_SECTION_CHARS = 400_000  # per section; a longer one is cut and marked


class RateLimiter:
    """Minimum spacing of 1/max_per_sec between requests; clock and sleep are injectable."""

    def __init__(
        self,
        max_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 1.0 / max_per_sec
        self._clock, self._sleep = clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            self._sleep(self.interval - (now - self._last))
            now = self._clock()
        self._last = now


def zero_cik(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


def resolve_cik(master: SecurityMaster, security_id: int) -> str:
    cik = master.cik_of(security_id)
    if cik is None:
        raise NiveshError(
            f"security {security_id} has no SEC CIK; only US listings from `nivesh master build` "
            "have one"
        )
    return cik


@dataclass(frozen=True)
class FilingRef:
    form: str
    filed_at: date
    period_end: date | None
    accession: str
    primary_doc: str

    def url(self, cik: str) -> str:
        acc = self.accession.replace("-", "")
        return DOC_URL.format(cik=int(cik), acc=acc, doc=self.primary_doc)


def parse_submissions(doc: Any, forms: tuple[str, ...] = FORMS) -> list[FilingRef]:
    """Recent filings of the given forms (amendments included), newest first."""
    recent = (doc.get("filings") or {}).get("recent") or {}
    cols = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")
    out = []
    for acc, filed, report, form, primary in zip(*(recent.get(c, []) for c in cols), strict=False):
        if form.removesuffix("/A") not in forms:
            continue
        out.append(
            FilingRef(
                form=form,
                filed_at=date.fromisoformat(filed),
                period_end=date.fromisoformat(report) if report else None,
                accession=acc,
                primary_doc=primary,
            )
        )
    return sorted(out, key=lambda f: f.filed_at, reverse=True)


def statements(doc: Any) -> QuarterisedFacts:
    """Company facts -> standardised annual/quarterly rows (see `engine.statements`)."""
    return quarterise(facts_from_companyfacts(doc))


# Sections ---------------------------------------------------------------------------------------


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


TEN_K = {"1": "business", "1a": "risk_factors", "3": "legal_proceedings", "7": "mdna",
         "7a": "market_risk", "8": "financial_statements"}  # fmt: skip
TEN_Q = {("I", "1"): "financial_statements", ("I", "2"): "mdna", ("I", "3"): "market_risk",
         ("II", "1"): "legal_proceedings", ("II", "1a"): "risk_factors"}  # fmt: skip
_ITEM = re.compile(r"^item\s+(\d+(?:\.\d+)?[a-z]?)\b", re.I)
_PART = re.compile(r"^part\s+(ii|i)\b", re.I)


def extract_sections(form: str, html: str) -> dict[str, str]:
    """Named sections of a 10-K, 10-Q or 8-K. Each heading line `Item N` starts a segment; for a
    repeated item (table of contents, then the body) the longest segment wins."""
    base = form.removesuffix("/A")
    lines = html_text(html).splitlines()
    marks: list[tuple[int, str]] = []
    part = "I"
    for i, line in enumerate(lines):
        if m := _PART.match(line):
            part = m[1].upper()
        if m := _ITEM.match(line):
            item = m[1].lower()
            if base == "10-Q":
                name = TEN_Q.get((part, item), f"part_{part.lower()}_item_{item}")
            elif base == "10-K":
                name = TEN_K.get(item, f"item_{item}")
            else:
                name = f"item_{item}"
            marks.append((i, name))
    out: dict[str, str] = {}
    for n, (start, name) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        text = "\n".join(lines[start:end])
        if len(text) > MAX_SECTION_CHARS:
            text = text[:MAX_SECTION_CHARS] + "\n[section truncated]"
        if len(text) > len(out.get(name, "")):
            out[name] = text
    return out


class Edgar(Adapter):
    name = "edgar"
    source = "sec_edgar"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_per_sec: float = 8,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._client = client or make_client(self.name)
        self._limiter = limiter or RateLimiter(max_per_sec)

    def _get(self, url: str) -> httpx.Response:
        headers = sec_headers()
        self._limiter.wait()
        resp = self._client.get(url, headers=headers)
        if resp.status_code == 429:
            raise RateLimited("SEC EDGAR rate-limited the request (HTTP 429); retry later")
        if resp.status_code != 200:
            raise SourceUnavailable(f"SEC EDGAR returned HTTP {resp.status_code}")
        return resp

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource = params["resource"]
        cik = zero_cik(params["cik"])
        if resource == "companyfacts":
            return self._get(FACTS_URL.format(cik=cik)).json(), utcnow()
        if resource == "submissions":
            return self._get(SUBMISSIONS_URL.format(cik=cik)).json(), utcnow()
        if resource == "document":
            ref = FilingRef(params["form"], date.min, None, params["accession"], params["doc"])
            html = self._get(ref.url(cik)).text
            return {"form": ref.form, "sections": extract_sections(ref.form, html)}, utcnow()
        raise ValueError(f"unknown resource {resource!r}")
