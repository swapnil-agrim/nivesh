"""News de-duplication (ST-4.7). Pure. Two items are the same story when their canonical URLs
match, or their titles are at least 90 percent similar and were published within 48 hours.
The earliest published item is kept; other sources that carried it are recorded."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

THRESHOLD = 0.9
WINDOW = timedelta(hours=48)
_PUNCT = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class Item:
    title: str
    url: str
    published: datetime
    source: str
    security_id: int | None = None
    kind: str = "news"


@dataclass
class Kept:
    item: Item
    also: list[str] = field(default_factory=list)  # other sources that carried the same story


@dataclass
class DedupeResult:
    kept: list[Kept] = field(default_factory=list)
    dropped: int = 0


def canonical_url(url: str) -> str:
    """Lowercase host, no fragment, no utm_* parameters, sorted query, no trailing slash
    (except the bare root)."""
    if not url:
        return ""
    p = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_"))
    path = p.path.rstrip("/") or ("/" if not p.query else "")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), ""))


def normalise_title(title: str, source: str = "") -> str:
    t = title.strip()
    if source:
        t = re.sub(rf"\s*[-|–—]\s*{re.escape(source)}\s*$", "", t, flags=re.I)
    return " ".join(_PUNCT.sub(" ", t.lower()).split())


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _same(a: Item, b: Item) -> bool:
    ua, ub = canonical_url(a.url), canonical_url(b.url)
    if ua and ua == ub:
        return True
    if abs(a.published - b.published) > WINDOW:
        return False
    if "announcement" in (a.kind, b.kind) and (
        a.security_id is None or a.security_id != b.security_id
    ):
        return False  # near-identical titles of different (or unknown) securities are not one story
    # a suffix naming either item's source is stripped from both titles
    ta = normalise_title(normalise_title(a.title, a.source), b.source)
    tb = normalise_title(normalise_title(b.title, b.source), a.source)
    return title_similarity(ta, tb) >= THRESHOLD


def dedupe(items: Sequence[Item], existing: Sequence[Item] = ()) -> DedupeResult:
    """Items that are new, earliest first; stories already in `existing` are dropped."""
    out = DedupeResult()
    for it in sorted(items, key=lambda i: i.published):
        if any(_same(it, e) for e in existing):
            out.dropped += 1
            continue
        twin = next((k for k in out.kept if _same(it, k.item)), None)
        if twin is None:
            out.kept.append(Kept(it))
            continue
        out.dropped += 1
        if it.source != twin.item.source and it.source not in twin.also:
            twin.also.append(it.source)
    for k in out.kept:
        k.also.sort()
    return out
