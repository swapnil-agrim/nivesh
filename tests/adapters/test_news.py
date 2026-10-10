import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nivesh_adapters.news import (
    Feeds,
    parse_bse_announcements,
    parse_feed,
    parse_nse_announcements,
)
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.errors import SourceUnavailable
from nivesh_core.pii_scan import scan_paths

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"


def test_rss2_items_parsed_with_pubdate_to_utc() -> None:
    got = parse_feed((FX / "rss_a.xml").read_text())
    first = got.items[0]
    assert first.title.startswith("Reliance Industries Q3 results")
    assert first.published == datetime(2026, 1, 5, 3, 30, tzinfo=UTC)  # +0530 -> UTC
    assert got.items[1].published == datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    assert first.url == "https://news.example.invalid/a/1?utm_source=rss"


def test_atom_entries_parsed_with_updated_to_utc() -> None:
    got = parse_feed((FX / "atom_b.xml").read_text())
    assert [i.url for i in got.items] == [
        "https://atom.example.invalid/b/1",
        "https://atom.example.invalid/b/2",
    ]
    assert got.items[0].published == datetime(2026, 1, 5, 4, 0, tzinfo=UTC)
    assert got.items[1].published == datetime(2026, 1, 5, 10, 0, tzinfo=UTC)  # published wins


def test_missing_or_bad_date_item_skipped_and_counted() -> None:
    got = parse_feed((FX / "rss_a.xml").read_text())
    assert len(got.items) == 3 and got.skipped == 2


def test_doctype_or_entity_feed_rejected() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>'
    with pytest.raises(DataQualityError, match="DOCTYPE"):
        parse_feed(bomb)
    with pytest.raises(DataQualityError, match="malformed"):
        parse_feed("<rss><channel>")
    with pytest.raises(DataQualityError, match="not an RSS or Atom"):
        parse_feed("<html/>")


def test_html_in_summary_stripped() -> None:
    got = parse_feed((FX / "rss_a.xml").read_text())
    assert got.items[1].summary == "Deal value undisclosed & multi-year."
    assert got.items[0].summary == "Quarterly results beat estimates."


def test_bse_announcements_json_parsed_with_scrip_code_and_meeting_date() -> None:
    rows = parse_bse_announcements(json.loads((FX / "bse_announcements.json").read_text()))
    assert rows[0].scrip_code == "500325" and rows[0].meeting_date.isoformat() == "2026-01-16"  # type: ignore[union-attr]
    assert rows[0].category == "Board Meeting"
    assert rows[0].published == datetime(2026, 1, 5, 5, 30, tzinfo=UTC)  # 11:00 IST
    assert rows[1].meeting_date is None
    with pytest.raises(DataQualityError, match="Table"):
        parse_bse_announcements({})
    with pytest.raises(DataQualityError, match="bad row"):
        parse_bse_announcements({"Table": [{"x": 1}]})


def test_nse_announcements_parsed() -> None:
    rows = parse_nse_announcements(
        [
            {
                "symbol": "TCS",
                "desc": "Financial Results",
                "an_dt": "05-Jan-2026 13:00:00",
                "attchmntText": "Outcome text",
            }
        ]  # fmt: skip
    )
    assert rows[0].symbol == "TCS" and rows[0].published == datetime(2026, 1, 5, 7, 30, tzinfo=UTC)
    with pytest.raises(DataQualityError, match="expected list"):
        parse_nse_announcements({})
    with pytest.raises(DataQualityError, match="bad row"):
        parse_nse_announcements([{"symbol": "X"}])


def feeds(handler: object) -> Feeds:
    return Feeds(httpx.Client(transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]


def test_feeds_fetch_resources_and_errors() -> None:
    xml = (FX / "rss_a.xml").read_text()
    bse = json.loads((FX / "bse_announcements.json").read_text())

    def handler(req: httpx.Request) -> httpx.Response:
        if "bse" in req.url.host:
            return httpx.Response(200, json=bse)
        if "down" in req.url.host:
            return httpx.Response(503)
        if "evil" in req.url.host:
            return httpx.Response(200, text="<!DOCTYPE x><rss/>")
        return httpx.Response(200, text=xml)

    f = feeds(handler)
    assert f.fetch(resource="rss", url="https://x.example/f.xml").data == xml
    assert f.fetch(resource="bse_announcements", url="https://bse.example/a").data == bse
    assert f.fetch(resource="nse_announcements", url="https://bse.example/a").data == bse
    with pytest.raises(SourceUnavailable):
        f.fetch(resource="rss", url="https://down.example/f.xml")
    with pytest.raises(DataQualityError, match="DOCTYPE"):
        f.fetch(resource="rss", url="https://evil.example/f.xml")
    with pytest.raises(ValueError, match="unknown resource"):
        f.fetch(resource="nope", url="https://x.example")
    with pytest.raises(FixtureMissing):
        Feeds().fetch(resource="rss", url="https://x.example/f.xml")


def test_news_fixtures_scan_clean_of_pii() -> None:
    names = ("rss_a.xml", "atom_b.xml", "bse_announcements.json")
    assert scan_paths([FX / n for n in names]) == []


# tagging ---------------------------------------------------------------------------------------


@pytest.fixture
def tagger(tmp_path: Path):  # type: ignore[no-untyped-def]
    from nivesh_adapters.news import Tagger
    from nivesh_core.db import init_stores
    from nivesh_core.db.sqlite import open_sqlite
    from nivesh_core.security_master import build_master
    from tests.market_fx import mrow

    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow("RELIANCE", name="Reliance Industries Limited", isin="INE002A01018",
                 bse_code="500325"),
            mrow("TCS", name="Tata Consultancy Services Limited", isin="INE467B01029",
                 bse_code="532540"),
            mrow("TATAMOTORS", name="Tata Motors Limited", isin="INE155A01022"),
            mrow("NIFTY 50", name="NIFTY 50", asset_class="index"),
        ],
        [],
    )  # fmt: skip
    yield Tagger(sql), sql
    sql.close()


def test_item_tagged_to_security_by_exact_symbol_token_or_full_name(tagger) -> None:  # type: ignore[no-untyped-def]
    t, sql = tagger
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    assert t.by_text("TCS bags large order") == ids["TCS"]
    assert t.by_text("Reliance Industries Q3 results beat") == ids["RELIANCE"]
    assert t.by_text("Shares of Tata Motors slip") == ids["TATAMOTORS"]
    assert t.by_text("tcs lowercase is not a ticker") is None
    assert t.by_text("Nifty 50 ends higher") is None  # indices are never tagged


def test_ambiguous_title_left_untagged_not_guessed(tagger) -> None:  # type: ignore[no-untyped-def]
    t, _ = tagger
    assert t.by_text("RELIANCE and TCS lead the gainers") is None
    assert t.by_text("Tata group stocks rally") is None
    assert t.by_text("") is None


def test_exchange_announcement_tagged_by_scrip_code_alias(tagger) -> None:  # type: ignore[no-untyped-def]
    t, sql = tagger
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    assert t.by_scrip("500325") == ids["RELIANCE"] and t.by_scrip("111111") is None
    assert t.by_symbol("TCS") == ids["TCS"] and t.by_symbol("ZZZ") is None
