import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from nivesh_adapters.market_ingest import ingest_news
from nivesh_adapters.news import Feeds
from nivesh_core.config import FeedSpec, MarketSettings, Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_store import get_events, get_news
from nivesh_core.pii_scan import scan_text
from nivesh_core.security_master import build_master
from tests.market_fx import mrow

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
NOW = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


@dataclass
class Env:
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    settings: Settings
    ids: dict[str, int]


def settings_for(tmp: Path, *kinds: tuple[str, str, str]) -> Settings:
    feeds = [FeedSpec(name=n, url=u, kind=k) for n, u, k in kinds]  # type: ignore[arg-type]
    return Settings(data_dir=str(tmp), market=MarketSettings(feeds=feeds))


FEEDS = (
    ("wire", "https://wire.example.invalid/rss.xml", "rss"),
    ("atomfeed", "https://atom.example.invalid/feed.xml", "rss"),
    ("bse", "https://bse.example.invalid/ann", "bse_announcements"),
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("RELIANCE", name="Reliance Industries Limited", isin="INE002A01018",
                 bse_code="500325"),
            mrow("TCS", name="Tata Consultancy Services Limited", isin="INE467B01029",
                 bse_code="532540"),
        ],
        [],
    )  # fmt: skip
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    duck = open_duck(tmp_path / "nivesh.duckdb")
    yield Env(sql, duck, settings_for(tmp_path, *FEEDS), ids)
    duck.close()
    sql.close()


class Net:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.host)
        if any(f in req.url.host for f in self.fail):
            return httpx.Response(503)
        if "wire" in req.url.host:
            return httpx.Response(200, text=(FX / "rss_a.xml").read_text())
        if "atom" in req.url.host:
            return httpx.Response(200, text=(FX / "atom_b.xml").read_text())
        return httpx.Response(200, json=json.loads((FX / "bse_announcements.json").read_text()))


def feeds(net: Net) -> Any:
    c = httpx.Client(transport=httpx.MockTransport(net))
    return lambda: Feeds(c)


def run(env: Env, net: Net, **kw: Any):  # type: ignore[no-untyped-def]
    return ingest_news(env.duck, env.sql, env.settings, feeds=feeds(net), now=NOW, **kw)


def test_ingest_news_dedupes_across_feeds_and_stores_classification(env: Env) -> None:
    rep = run(env, Net())
    # rss: 3 usable, atom: 2, bse: 3 -> 8 candidates, one cross-feed duplicate
    assert rep.fetched == 8 and rep.duplicates == 1 and rep.new == 7 and rep.skipped_dates == 2
    rows = get_news(env.duck)
    results = [r for r in rows if r.title.startswith("Reliance Industries Q3 results")]
    assert len(results) == 1 and results[0].source == "wire"  # earliest published kept
    assert (results[0].event_type, results[0].materiality) == ("results", "high")
    assert results[0].security_id == env.ids["RELIANCE"] and results[0].kind == "news"
    assert results[0].classified_by == "rules" and results[0].sentiment == "positive"


def test_ingest_news_stores_published_at_utc_and_source(env: Env) -> None:
    run(env, Net())
    by_title = {r.title: r for r in get_news(env.duck)}
    tcs = by_title["TCS bags large order from European retailer"]
    assert tcs.published_at == datetime(2026, 1, 5, 10, 0, tzinfo=UTC) and tcs.source == "wire"
    assert tcs.security_id == env.ids["TCS"] and tcs.event_type == "contract_win"
    assert tcs.summary == "Deal value undisclosed & multi-year."
    ann = by_title["TCS - Outcome of Board Meeting"]
    assert ann.kind == "announcement" and ann.security_id == env.ids["TCS"]
    assert ann.published_at == datetime(2026, 1, 5, 7, 0, tzinfo=UTC)  # 12:30 IST


def test_ambiguous_and_unknown_items_are_untagged(env: Env) -> None:
    rep = run(env, Net())
    by_title = {r.title: r for r in get_news(env.duck)}
    assert by_title["RELIANCE and TCS lead the gainers"].security_id is None
    assert by_title["Unknown scrip announcement"].security_id is None
    assert rep.untagged == 2


def test_board_meeting_intimation_creates_results_calendar_event(env: Env) -> None:
    rep = run(env, Net())
    ev = get_events(env.duck, security_id=env.ids["RELIANCE"])
    assert [(e.event_type, e.event_date, e.source) for e in ev] == [
        ("results", date(2026, 1, 16), "bse")
    ]
    assert rep.events == 1


def test_ingest_news_failed_feed_reported_others_stored(env: Env) -> None:
    net = Net()
    net.fail = {"atom"}
    rep = run(env, net)
    assert any("atomfeed" in f for f in rep.failed)
    assert rep.new == 6 and rep.duplicates == 0 and len(get_news(env.duck)) == 6


def test_ingest_news_uses_news_ttl(env: Env) -> None:
    net = Net()
    run(env, net)
    n = len(net.calls)
    run(env, net)
    assert len(net.calls) == n == 3


def test_reingest_adds_nothing(env: Env) -> None:
    net = Net()
    first = run(env, net)
    again = run(env, net, refresh=True)
    assert again.new == 0 and again.duplicates == first.fetched
    assert len(get_news(env.duck)) == first.new


def test_news_filters_and_pagination(env: Env) -> None:
    run(env, Net())
    tcs = env.ids["TCS"]
    only = get_news(env.duck, security_id=tcs)
    assert only and all(r.security_id == tcs for r in only)
    assert [r.published_at for r in only] == sorted((r.published_at for r in only), reverse=True)
    since = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert all(r.published_at >= since for r in get_news(env.duck, since=since))
    assert len(get_news(env.duck, limit=2)) == 2
    assert get_news(env.duck, limit=2, offset=1)[0] == get_news(env.duck)[1]


def test_stored_news_fixture_text_has_no_pii_scan_hits(env: Env) -> None:
    run(env, Net())
    text = "\n".join(f"{r.title} {r.summary}" for r in get_news(env.duck))
    assert scan_text(text) == []


def test_no_feeds_configured_is_an_error(env: Env) -> None:
    from nivesh_core.errors import NiveshError

    with pytest.raises(NiveshError, match="no news feeds configured"):
        ingest_news(env.duck, env.sql, Settings(), feeds=feeds(Net()), now=NOW)
