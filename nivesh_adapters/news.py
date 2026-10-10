"""News feeds and exchange announcements (ST-4.7): RSS 2.0 and Atom with the stdlib XML parser,
the BSE announcements JSON and the NSE announcements JSON.

A feed containing a DOCTYPE or ENTITY declaration is rejected before parsing (entity-expansion
defence). Titles and summaries are external, untrusted text: they are stored as received (HTML
stripped from summaries) and wrapped as untrusted at the MCP boundary. Wire formats are per spec,
unverified against live sources (follow-up D2). `_fetch` returns text or parsed JSON; models are
built by the `parse_*` functions after the cache.
"""

import re
import sqlite3
import xml.etree.ElementTree as ET  # noqa: S405 - DOCTYPE/ENTITY rejected before parsing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.edgar import html_text
from nivesh_adapters.prices_in import http_get
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import SourceUnavailable
from nivesh_core.security_master import normalise_name
from nivesh_core.timeutil import IST, utcnow

_FORBIDDEN = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    published: datetime  # UTC
    summary: str


@dataclass
class ParsedFeed:
    items: list[FeedItem] = field(default_factory=list)
    skipped: int = 0  # items with a missing or unreadable date


@dataclass(frozen=True)
class Announcement:
    title: str
    published: datetime  # UTC
    url: str | None = None
    category: str = ""
    scrip_code: str | None = None  # BSE
    symbol: str | None = None  # NSE
    meeting_date: date | None = None
    text: str = ""


def check_feed_text(text: str, what: str = "feed") -> None:
    if _FORBIDDEN.search(text):
        raise DataQualityError("news", what, "<!DOCTYPE/ENTITY>", "declarations are not accepted")


def _plain(html: str | None) -> str:
    return " ".join(html_text(html or "").split())


def _child(el: ET.Element, *names: str) -> str:
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return ""


def _utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return _utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        pass
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def parse_feed(text: str) -> ParsedFeed:
    """RSS 2.0 or Atom -> items with UTC publication times; undated items are skipped, counted."""
    check_feed_text(text)
    try:
        root = ET.fromstring(text)  # noqa: S314 - DOCTYPE/ENTITY rejected above
    except ET.ParseError as e:
        raise DataQualityError("news", "feed", str(e)[:60], "malformed XML") from None
    out = ParsedFeed()
    channel = _child(root, "channel/title", f"{ATOM}title")
    if root.tag == "rss":
        raw = [
            (
                _child(i, "title"),
                _child(i, "link"),
                _date(_child(i, "pubDate")),
                _child(i, "description"),
            )
            for i in root.iter("item")
        ]
    elif root.tag == f"{ATOM}feed":
        raw = []
        for entry in root.iter(f"{ATOM}entry"):
            link = entry.find(f"{ATOM}link")
            raw.append(
                (
                    _child(entry, f"{ATOM}title"),
                    link.get("href", "") if link is not None else "",
                    _date(_child(entry, f"{ATOM}published", f"{ATOM}updated")),
                    _child(entry, f"{ATOM}summary"),
                )
            )
    else:
        raise DataQualityError("news", "feed", root.tag, "not an RSS or Atom document")
    for title, url, when, summary in raw:
        if when is None or not title:
            out.skipped += 1
            continue
        if channel:  # "Headline - Channel Name" is the same headline
            title = re.sub(rf"\s+[-|]\s+{re.escape(channel)}$", "", title, flags=re.I)
        out.items.append(FeedItem(title, url, when, _plain(summary)))
    return out


def _ist(text: str, fmt: str | None = None) -> datetime:
    naive = datetime.strptime(text.strip(), fmt) if fmt else datetime.fromisoformat(text.strip())
    return naive.replace(tzinfo=IST).astimezone(UTC)


def parse_bse_announcements(doc: Any) -> list[Announcement]:
    rows = doc.get("Table") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise DataQualityError("bse_announcements", "document", type(doc).__name__, "no Table list")
    out = []
    for i, r in enumerate(rows):
        try:
            meeting = (r.get("MEETING_DATE") or "").strip()
            out.append(
                Announcement(
                    title=str(r["HEADLINE"]).strip(),
                    published=_ist(r["NEWS_DT"]),
                    url=r.get("NSURL") or None,
                    category=str(r.get("CATEGORYNAME", "")),
                    scrip_code=str(r["SCRIP_CD"]).strip(),
                    meeting_date=date.fromisoformat(meeting) if meeting else None,
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise DataQualityError("bse_announcements", f"row {i}", r, "bad row") from None
    return out


def parse_nse_announcements(doc: Any) -> list[Announcement]:
    if not isinstance(doc, list):
        raise DataQualityError("nse_announcements", "document", type(doc).__name__, "expected list")
    out = []
    for i, r in enumerate(doc):
        try:
            out.append(
                Announcement(
                    title=f"{r['symbol']} - {r['desc']}",
                    published=_ist(r["an_dt"], "%d-%b-%Y %H:%M:%S"),
                    category=str(r["desc"]),
                    symbol=str(r["symbol"]).strip(),
                    text=str(r.get("attchmntText", "")),
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise DataQualityError("nse_announcements", f"row {i}", r, "bad row") from None
    return out


class Feeds(Adapter):
    name = "news"
    source = "news"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource, url = params["resource"], params["url"]
        if resource not in ("rss", "bse_announcements", "nse_announcements"):
            raise ValueError(f"unknown resource {resource!r}")
        resp = http_get(self._client, url)
        if resp is None:
            raise SourceUnavailable(f"{httpx.URL(url).host} has nothing at that address")
        if resource == "rss":
            check_feed_text(resp.text, httpx.URL(url).host)
            return resp.text, utcnow()
        return resp.json(), utcnow()


class Tagger:
    """Attach news to a security only when the match is unambiguous: an upper-case ticker token
    (three letters or more) or the full normalised company name. Two different securities in one
    headline, or none, leaves it untagged. Indices and funds are never tagged."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._symbols: dict[str, set[int]] = {}
        self._names: list[tuple[str, int]] = []
        rows = conn.execute(
            "SELECT id, symbol, name, name_norm FROM security WHERE unresolved = 0 "
            "AND COALESCE(asset_class, '') NOT IN ('mf', 'index')"
        ).fetchall()
        for sid, symbol, name, name_norm in rows:
            self._symbols.setdefault(symbol.upper(), set()).add(sid)
            norm = name_norm or normalise_name(name or "")
            if len(norm) >= 4:
                self._names.append((f" {norm} ", sid))
        self._alias: dict[tuple[str, str], int] = {
            (kind, value): sid
            for kind, value, sid in conn.execute(
                "SELECT kind, value, security_id FROM security_alias "
                "WHERE kind IN ('bse_code', 'symbol')"
            )
        }

    def by_text(self, title: str) -> int | None:
        found: set[int] = set()
        for token in re.findall(r"[A-Za-z0-9&]+", title):
            if len(token) >= 3 and token.isupper():
                found |= self._symbols.get(token, set())
        padded = f" {normalise_name(title)} "
        found |= {sid for name, sid in self._names if name in padded}
        return next(iter(found)) if len(found) == 1 else None

    def by_scrip(self, code: str) -> int | None:
        return self._alias.get(("bse_code", code))

    def by_symbol(self, symbol: str) -> int | None:
        ids = self._symbols.get(symbol.upper(), set())
        return next(iter(ids)) if len(ids) == 1 else self._alias.get(("symbol", symbol.upper()))
