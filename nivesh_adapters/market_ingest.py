"""Market-data ingest: adapter -> `cached_fetch(redact=False)` -> parse -> `market_store`.

The only module (besides `market_store`) that writes market tables. Adapters return JSON-native
data, models are built after the cache, so a cache hit and a fresh fetch parse identically.
Source failures (HTTP, rate limit, offline) are recorded in the report and the run continues;
malformed data (`DataQualityError`) aborts the run before anything is written.
"""

import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import duckdb
import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.cache import cached_fetch
from nivesh_adapters.edgar import (
    FORMS,
    Edgar,
    FilingRef,
    parse_submissions,
    resolve_cik,
    statements,
)
from nivesh_adapters.estimates import (
    Estimates,
    check_available,
    parse_fmp_calendar,
    parse_fmp_estimates,
)
from nivesh_adapters.fundamentals_in import (
    IndiaFundamentals,
    parse_results_xbrl,
    parse_shareholding,
    prefer_consolidated,
)
from nivesh_adapters.macro import MacroFetch, parse_flows, parse_fred
from nivesh_adapters.news import (
    Feeds,
    Tagger,
    parse_bse_announcements,
    parse_feed,
    parse_nse_announcements,
)
from nivesh_adapters.prices_in import (
    EQUITY_SERIES,
    IndiaPrices,
    parse_bhavcopy,
    parse_corp_actions,
    parse_indices,
    parse_yahoo,
)
from nivesh_adapters.prices_us import UsPrices, parse_stooq
from nivesh_adapters.quality import DataQualityError
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError, SecretNotFound
from nivesh_core.market_models import CorpAction, PriceBar
from nivesh_core.market_store import (
    NewsRow,
    bars_by_source,
    get_corp_actions,
    get_macro,
    get_news,
    get_shareholding,
    get_statement_rows,
    save_filing,
    write_estimates,
    write_events,
    write_fundamentals,
    write_macro,
    write_news,
    write_prices,
)
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_core.timeutil import utcnow
from nivesh_engine.adjust import adjust_closes, cross_check
from nivesh_engine.calendar import Calendar, find_gaps
from nivesh_engine.classify import Classifier, RuleClassifier, classify_item
from nivesh_engine.dedupe import Item, dedupe

SPLIT_ADJUSTED = {"yahoo", "stooq"}  # closes already split-adjusted at the source
INDIA_YAHOO_INDEX = {
    "NIFTY 50": "^NSEI",
    "NIFTY BANK": "^NSEBANK",
    "INDIA VIX": "^INDIAVIX",
    "SENSEX": "^BSESN",
}


@dataclass
class Run:
    """Shared state of one ingest call: stores, settings, report bookkeeping."""

    duck: duckdb.DuckDBPyConnection
    settings: Settings
    refresh: bool = False
    stale: bool = False
    failed: list[str] = field(default_factory=list)

    def get(
        self, adapter: Adapter, params: dict[str, Any], data_type: str, label: str = ""
    ) -> Any | None:
        """Cached fetch; a source failure is recorded and gives None (bad data still raises)."""
        try:
            res = cached_fetch(
                adapter, params, data_type, conn=self.duck, ttls=self.settings.ttls,
                refresh=self.refresh, redact=False,
            )  # fmt: skip
        except DataQualityError:
            raise
        except (NiveshError, httpx.HTTPError) as e:
            keys = ("day", "symbol", "scrip", "series_id")
            what = label or next((params[k] for k in keys if params.get(k)), "")
            self.failed.append(f"{params['resource']} {what}: {e}".replace("  ", " "))
            return None
        self.stale |= res.stale
        return res.data


@dataclass
class PriceReport:
    security_id: int
    symbol: str
    bars: int = 0
    flags: dict[str, int] = field(default_factory=dict)
    gaps: list[date] = field(default_factory=list)
    actions: int = 0
    stale: bool = False
    failed: list[str] = field(default_factory=list)


def calendar_for(market: str, settings: Settings) -> Calendar:
    if market == "US":
        return Calendar.nyse()
    return Calendar.nse({y: frozenset(v) for y, v in settings.market.nse_holidays.items()})


def india_yahoo_symbol(sec: SecurityRow) -> str:
    if sec.asset_class == "index":
        return INDIA_YAHOO_INDEX.get(sec.symbol.upper(), sec.symbol)
    return sec.symbol + (".BO" if sec.exchange == "BSE" else ".NS")


@dataclass
class _Fetched:
    primary: list[PriceBar] = field(default_factory=list)
    secondary: list[PriceBar] = field(default_factory=list)
    actions: list[CorpAction] = field(default_factory=list)
    primary_raw: bool = True


def _india(run: Run, sec: SecurityRow, days: list[date], ip: IndiaPrices) -> _Fetched:
    out = _Fetched()
    index = sec.asset_class == "index"
    src = "bse_bhavcopy" if sec.exchange == "BSE" else "nse_bhavcopy"
    for d in days:
        data = run.get(ip, {"resource": "indices" if index else src, "day": d.isoformat()}, "price")
        if not data:
            continue
        if index:
            out.primary += parse_indices(data, {sec.symbol.upper(): sec.id}).bars
        else:
            series = EQUITY_SERIES if src == "nse_bhavcopy" else None
            ids = {sec.symbol: sec.id}
            out.primary += parse_bhavcopy(
                data, ids, key_col="TckrSymb", source=src, series=series
            ).bars
    if days:
        start, end = days[0].isoformat(), days[-1].isoformat()
        params = {
            "resource": "yahoo",
            "symbol": india_yahoo_symbol(sec),
            "start": start,
            "end": end,
        }
        chart = run.get(ip, params, "price")
        if chart is not None:
            out.secondary, out.actions = parse_yahoo(chart, sec.id)
        if not index and sec.exchange == "NSE":
            first, last = (d.strftime("%d-%m-%Y") for d in (days[0], days[-1]))
            params = {"resource": "corp_actions", "symbol": sec.symbol, "start": first, "end": last}
            rows = run.get(ip, params, "price")
            if rows is not None:
                out.actions += parse_corp_actions(rows, sec.id)
    return out


def _us(run: Run, sec: SecurityRow, start: date, end: date, up: UsPrices) -> _Fetched:
    out = _Fetched(primary_raw=False)
    rng = {"symbol": sec.symbol, "start": start.isoformat(), "end": end.isoformat()}
    chart = run.get(up, {"resource": "yahoo", **rng}, "price")
    if chart is not None:
        out.primary, out.actions = parse_yahoo(chart, sec.id)
    if run.settings.market.us_secondary == "stooq":
        text = run.get(up, {"resource": "stooq", **rng}, "price")
        if text:
            out.secondary = parse_stooq(text, sec.id)
    return out


def ingest_prices(
    duck: duckdb.DuckDBPyConnection,
    settings: Settings,
    sec: SecurityRow,
    start: date,
    end: date,
    *,
    refresh: bool = False,
    india: Callable[[], IndiaPrices] = IndiaPrices,
    us: Callable[[], UsPrices] = UsPrices,
) -> PriceReport:
    """Fetch, cross-check, adjust and store EOD bars for one security over [start, end]."""
    if start > end:
        raise NiveshError(f"start {start} is after end {end}")
    if sec.asset_class == "mf":
        raise NiveshError(f"{sec.symbol} is a mutual fund; NAVs come from `nivesh mf nav`")
    cal = calendar_for(sec.market, settings)
    days = cal.sessions(start, end)  # CalendarUnknown for an NSE year without holiday data
    run = Run(duck, settings, refresh)
    got = _india(run, sec, days, india()) if sec.market != "US" else _us(run, sec, start, end, us())
    got.primary = [b for b in got.primary if start <= b.date <= end]
    got.secondary = [b for b in got.secondary if start <= b.date <= end]

    stored = {(a.ex_date, a.kind, a.source): a for a in get_corp_actions(duck, sec.id)}
    stored.update({(a.ex_date, a.kind, a.source): a for a in got.actions})
    actions = sorted(stored.values(), key=lambda a: a.ex_date)
    flagged = cross_check(
        got.primary, got.secondary, actions, settings.market.cross_check_tolerance,
        primary_raw=got.primary_raw,
    )  # fmt: skip

    merged: dict[str, dict[date, PriceBar]] = {}
    for src, old in bars_by_source(duck, sec.id).items():
        merged[src] = {b.date: b for b in old}
    for b in [*flagged, *got.secondary]:
        merged.setdefault(b.source, {})[b.date] = b
    final: list[PriceBar] = []
    for src, by_day in merged.items():
        final += adjust_closes(list(by_day.values()), actions, splits=src not in SPLIT_ADJUSTED)
    write_prices(duck, final, got.actions)

    have = {b.date for b in final if start <= b.date <= end}
    return PriceReport(
        security_id=sec.id,
        symbol=sec.symbol,
        bars=len(flagged) + len(got.secondary),
        flags=dict(Counter(b.flag or "none" for b in flagged)),
        gaps=find_gaps(cal, have, start, end),
        actions=len(got.actions),
        stale=run.stale,
        failed=run.failed,
    )


# Fundamentals and filings (ST-4.1/4.4/4.5) ----------------------------------------------------


@dataclass
class FundamentalsReport:
    security_id: int
    symbol: str
    rows: int = 0  # new statement rows written (restatements count, repeats do not)
    annual_periods: int = 0
    quarterly_periods: int = 0
    shareholding: int = 0
    skipped: list[str] = field(default_factory=list)
    stale: bool = False
    failed: list[str] = field(default_factory=list)


@dataclass
class FilingsReport:
    security_id: int
    symbol: str
    listed: int = 0
    with_sections: int = 0
    stale: bool = False
    failed: list[str] = field(default_factory=list)


def _count(duck: duckdb.DuckDBPyConnection, table: str, sid: int) -> int:
    row = duck.execute(f"SELECT count(*) FROM {table} WHERE security_id = ?", (sid,)).fetchone()  # noqa: S608
    return int(row[0]) if row else 0


def _depth(duck: duckdb.DuckDBPyConnection, sid: int) -> tuple[int, int]:
    rows = get_statement_rows(duck, sid)
    return (
        len({r.period_end for r in rows if r.period_type == "A"}),
        len({r.period_end for r in rows if r.period_type == "Q"}),
    )


MAX_RESULT_FILINGS = 32  # about eight years of quarterly results


def ingest_fundamentals(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    sec: SecurityRow,
    *,
    refresh: bool = False,
    edgar: Callable[[], Edgar] | None = None,
    india: Callable[[], IndiaFundamentals] = IndiaFundamentals,
) -> FundamentalsReport:
    """Fetch and store standardised statements (and India shareholding) for one security.
    Append-only by `filed_at`; all rows land in one transaction."""
    if sec.asset_class in ("mf", "index"):
        kind = "mutual fund" if sec.asset_class == "mf" else "index"
        raise NiveshError(f"{sec.symbol} is a {kind}; fundamentals apply to listed companies")
    run = Run(duck, settings, refresh)
    master = SecurityMaster(sql)
    rep = FundamentalsReport(sec.id, sec.symbol)
    before = _count(duck, "fundamental", sec.id)
    if sec.market == "US":
        cik = resolve_cik(master, sec.id)
        ed = (edgar or (lambda: Edgar(max_per_sec=settings.market.edgar_max_per_sec)))()
        facts = run.get(ed, {"resource": "companyfacts", "cik": cik}, "fundamentals")
        if facts is not None:
            q = statements(facts)
            rep.skipped = q.skipped
            write_fundamentals(duck, sec.id, q.rows, ed.source)
    else:
        code = master.alias_of(sec.id, "bse_code")
        if code is None:
            raise NiveshError(f"{sec.symbol} has no BSE scrip code; run `nivesh master build`")
        fi = india()
        index = run.get(fi, {"resource": "results_index", "scrip": code}, "fundamentals")
        filings = []
        for item in sorted(index or [], key=lambda i: i["period_end"], reverse=True)[
            :MAX_RESULT_FILINGS
        ]:
            doc = run.get(
                fi,
                {"resource": "results_xbrl", "url": item["xbrl_url"], "filed_at": item["filed_at"]},
                "fundamentals",
            )
            if doc is not None:
                filings.append(parse_results_xbrl(doc["xml"], date.fromisoformat(doc["filed_at"])))
        rows, _basis = prefer_consolidated(filings)
        rep.skipped = [s for f in filings for s in f.skipped]
        raw = run.get(fi, {"resource": "shareholding", "scrip": code}, "fundamentals")
        holding = parse_shareholding(raw) if raw is not None else []
        write_fundamentals(duck, sec.id, rows, fi.source, shareholding=holding)
        rep.shareholding = len(get_shareholding(duck, sec.id))
    rep.rows = _count(duck, "fundamental", sec.id) - before
    rep.annual_periods, rep.quarterly_periods = _depth(duck, sec.id)
    rep.stale, rep.failed = run.stale, run.failed
    return rep


def _doc_key(ref: FilingRef) -> str:
    return ref.accession


def ingest_filings(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    sec: SecurityRow,
    *,
    forms: tuple[str, ...] = FORMS,
    per_form: int = 2,
    refresh: bool = False,
    edgar: Callable[[], Edgar] | None = None,
) -> FilingsReport:
    """List SEC filings of the given forms and store the named text sections of the newest
    `per_form` filings of each form (amendments count as their own form)."""
    run = Run(duck, settings, refresh)
    cik = resolve_cik(SecurityMaster(sql), sec.id)
    ed = (edgar or (lambda: Edgar(max_per_sec=settings.market.edgar_max_per_sec)))()
    rep = FilingsReport(sec.id, sec.symbol)
    subs = run.get(ed, {"resource": "submissions", "cik": cik}, "filings")
    taken: Counter[str] = Counter()
    for ref in parse_submissions(subs, forms) if subs is not None else []:
        sections = None
        if taken[ref.form] < per_form:
            taken[ref.form] += 1
            params = {"resource": "document", "cik": cik, "accession": ref.accession,
                      "doc": ref.primary_doc, "form": ref.form}  # fmt: skip
            doc = run.get(ed, params, "filings")
            sections = doc["sections"] if doc is not None else None
        save_filing(
            duck, security_id=sec.id, form=ref.form, filed_at=ref.filed_at,
            period_end=ref.period_end, source=ed.source, url=ref.url(cik),
            doc_key=_doc_key(ref), sections=sections,
        )  # fmt: skip
        rep.listed += 1
        rep.with_sections += bool(sections)
    rep.stale, rep.failed = run.stale, run.failed
    return rep


# Macro (ST-4.6) --------------------------------------------------------------------------------


@dataclass
class MacroReport:
    new: dict[str, int] = field(default_factory=dict)  # role -> points not stored before
    stale: bool = False
    failed: list[str] = field(default_factory=list)


def ingest_macro(
    duck: duckdb.DuckDBPyConnection,
    settings: Settings,
    *,
    roles: list[str] | None = None,
    refresh: bool = False,
    fetch: Callable[[], MacroFetch] | None = None,
) -> MacroReport:
    """Fetch the configured macro series (all roles, or the given ones) into `macro_series`.
    Series are stored under their role name; one failing role does not stop the others."""
    configured = settings.market.macro_series
    if not configured:
        raise NiveshError("no macro series configured; see market.macro_series in nivesh.yaml")
    wanted = roles or list(configured)
    for r in wanted:
        if r not in configured:
            raise NiveshError(f"unknown role {r!r}; configured roles: {', '.join(configured)}")
    fetcher = (fetch or (lambda: MacroFetch(key_ref=settings.market.fred_api_key)))()
    run = Run(duck, settings, refresh)
    rep = MacroReport()
    flows: list[Any] | None = None
    for role in wanted:
        spec = configured[role]  # type: ignore[index]
        before = len(get_macro(duck, role))
        try:
            if spec.source == "fred":
                params = {"resource": "fred", "series_id": spec.id, "years": 3}
                doc = run.get(fetcher, params, "macro", role)
                if doc is None:
                    continue
                points = [(o.date, o.value) for o in parse_fred(doc) if o.value is not None]
            else:
                if flows is None:
                    doc = run.get(fetcher, {"resource": "nse_flows"}, "macro")
                    if doc is None:
                        continue
                    flows = parse_flows(doc)
                points = [(f.day, f.net) for f in flows if f.category == spec.id]
        except SecretNotFound as e:  # a missing key fails this role only
            run.failed.append(f"{role}: {e}")
            continue
        write_macro(duck, role, points, spec.source)
        rep.new[role] = len(get_macro(duck, role)) - before
    rep.stale, rep.failed = run.stale, run.failed
    return rep


# Estimates and the event calendar (ST-4.9) -----------------------------------------------------


@dataclass
class EstimatesReport:
    security_id: int
    symbol: str
    available: bool = True
    reason: str | None = None
    stored: int = 0
    next_earnings: date | None = None
    stale: bool = False
    failed: list[str] = field(default_factory=list)


def ingest_estimates(
    duck: duckdb.DuckDBPyConnection,
    settings: Settings,
    sec: SecurityRow,
    *,
    today: date | None = None,
    refresh: bool = False,
    estimates: Callable[[], Estimates] | None = None,
) -> EstimatesReport:
    """Store today's estimate snapshot and upcoming earnings dates (US). India and a missing key
    give an unavailable report with the reason; nothing is stored."""
    rep = EstimatesReport(sec.id, sec.symbol)
    marker = check_available(sec.market, settings.market.fmp_api_key)
    if marker is not None:
        rep.available, rep.reason = False, marker.reason
        return rep
    day = today or utcnow().date()
    ad = (estimates or (lambda: Estimates(key_ref=settings.market.fmp_api_key)))()
    run = Run(duck, settings, refresh)
    doc = run.get(ad, {"resource": "estimates", "symbol": sec.symbol}, "estimates")
    if doc is not None:
        points = parse_fmp_estimates(doc, day)
        write_estimates(
            duck, sec.id, [(p.metric, p.period, p.value) for p in points], day, ad.source
        )
        rep.stored = len(points)
    cal = run.get(ad, {"resource": "earnings_calendar", "symbol": sec.symbol}, "estimates")
    if cal is not None:
        dates = parse_fmp_calendar(cal, day)
        write_events(duck, [(sec.id, "results", d) for d in dates], ad.source, day)
        rep.next_earnings = dates[0] if dates else None
    rep.stale, rep.failed = run.stale, run.failed
    return rep


# News and announcements (ST-4.7) ---------------------------------------------------------------


@dataclass
class NewsReport:
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    untagged: int = 0
    skipped_dates: int = 0
    events: int = 0
    stale: bool = False
    failed: list[str] = field(default_factory=list)


@dataclass
class _Cand:
    item: Item
    kind: str
    security_id: int | None
    summary: str
    meeting: date | None = None
    board_meeting: bool = False


def _announcement(a: Any, feed: str, tagger: Tagger) -> _Cand:
    sid = tagger.by_scrip(a.scrip_code) if a.scrip_code else None
    if sid is None and a.symbol:
        sid = tagger.by_symbol(a.symbol)
    return _Cand(
        Item(a.title, a.url or "", a.published, feed, sid, "announcement"), "announcement", sid,
        a.text,
        a.meeting_date, "board meeting" in a.category.lower(),
    )  # fmt: skip


def ingest_news(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    *,
    refresh: bool = False,
    feeds: Callable[[], Feeds] = Feeds,
    classifier: Classifier | None = None,
    now: datetime | None = None,
) -> NewsReport:
    """Fetch the configured feeds, tag, de-duplicate (across feeds and against stored items),
    classify and store. A failing feed is reported; the others are still stored."""
    specs = settings.market.feeds
    if not specs:
        raise NiveshError("no news feeds configured; see market.feeds in nivesh.yaml")
    run, rep, tagger, adapter = Run(duck, settings, refresh), NewsReport(), Tagger(sql), feeds()
    cands: list[_Cand] = []
    for spec in specs:
        doc = run.get(adapter, {"resource": "rss" if spec.kind == "rss" else spec.kind,
                                "url": spec.url}, "news", spec.name)  # fmt: skip
        if doc is None:
            continue
        if spec.kind == "rss":
            parsed = parse_feed(doc)
            rep.skipped_dates += parsed.skipped
            cands += [
                _Cand(Item(i.title, i.url, i.published, spec.name), "news",
                      tagger.by_text(i.title), i.summary)
                for i in parsed.items
            ]  # fmt: skip
        elif spec.kind == "bse_announcements":
            cands += [_announcement(a, spec.name, tagger) for a in parse_bse_announcements(doc)]
        else:
            cands += [_announcement(a, spec.name, tagger) for a in parse_nse_announcements(doc)]
    rep.fetched = len(cands)
    by_id = {id(c.item): c for c in cands}
    since = min((c.item.published for c in cands), default=None)
    existing = (
        [Item(r.title, r.url or "", r.published_at, r.source, r.security_id, r.kind)
         for r in get_news(duck, since=since - timedelta(hours=48))]
        if since else []
    )  # fmt: skip
    result = dedupe([c.item for c in cands], existing)
    rep.duplicates = rep.fetched - len(result.kept)
    clf = classifier or RuleClassifier()
    rows, events = [], []
    for k in result.kept:
        c = by_id[id(k.item)]
        cl = classify_item(clf, c.item.title, c.summary)
        rows.append(NewsRow(
            kind=c.kind, published_at=c.item.published, source=c.item.source, title=c.item.title,
            url=c.item.url or None, summary=c.summary or None, event_type=cl.event_type,
            materiality=cl.materiality, sentiment=cl.sentiment, classified_by=cl.classified_by,
            security_id=c.security_id,
        ))  # fmt: skip
        if c.board_meeting and c.meeting and c.security_id is not None:
            events.append((c.security_id, "results", c.meeting, c.item.source))
    stamp = now or utcnow()
    write_news(duck, rows, stamp)
    for src in {e[3] for e in events}:
        write_events(duck, [(s, t, d) for s, t, d, o in events if o == src], src, stamp.date())
    rep.new, rep.events = len(rows), len(events)
    rep.untagged = sum(1 for r in rows if r.security_id is None)
    rep.stale, rep.failed = run.stale, run.failed
    return rep
